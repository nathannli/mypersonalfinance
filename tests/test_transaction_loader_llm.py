import argparse
import os
import unittest
from types import SimpleNamespace
from typing import cast
from unittest.mock import Mock, patch

import polars as pl

from cli.transaction_loader_cli import TransactionLoaderCLI
from config import Config
from services.categorizer_factory import WriteAuthorizers
from services.llm_categorizer import OpenCodexCategorizer
from services.transaction_llm_approval import ApprovalError
from services.transaction_categorization import (
    Resolution,
    TransactionOutcome,
    TransactionStatus,
)
from services.transaction_processor import TransactionProcessor


class TestTransactionLlmConfig(unittest.TestCase):
    def test_defaults_to_shadow_with_pinned_model(self):
        # Config() loads the repository .env, so the file must be isolated for
        # this to assert the code defaults rather than the local configuration.
        environment = {
            "POSTGRES_CONNECTION_STRING": "postgresql://example",
        }
        with patch.dict(os.environ, environment, clear=True):
            with patch("config.load_dotenv"):
                config = Config()

        self.assertEqual(config.opencodex_base_url, "http://localhost:10100")
        self.assertEqual(config.opencodex_api_key, "")
        self.assertEqual(
            config.transaction_llm_model,
            "SingularityApiDev/deepseek-v4-flash-0731",
        )
        self.assertEqual(config.transaction_llm_timeout_seconds, 120.0)
        self.assertEqual(config.transaction_llm_mode, "shadow")

    def test_defaults_the_enriched_model_without_touching_the_legacy_one(self):
        with patch.dict(
            os.environ,
            {"POSTGRES_CONNECTION_STRING": "postgresql://example"},
            clear=True,
        ):
            config = Config()

        self.assertEqual(
            config.enriched_transaction_llm_model, "anthropic/claude-haiku-4-5"
        )
        self.assertEqual(
            config.transaction_llm_model,
            "SingularityApiDev/deepseek-v4-flash-0731",
        )

    def test_reads_the_enriched_model_override(self):
        environment = {
            "TRANSACTION_LLM_MODEL": "legacy/model",
            "ENRICHED_TRANSACTION_LLM_MODEL": "enriched/model",
        }
        with patch.dict(os.environ, environment, clear=True):
            config = Config()

        self.assertEqual(config.transaction_llm_model, "legacy/model")
        self.assertEqual(config.enriched_transaction_llm_model, "enriched/model")

    def test_config_never_reads_the_tinyfish_key(self):
        # V39: only the research entry point resolves TINYFISH_API_KEY.
        environment = {
            "POSTGRES_CONNECTION_STRING": "postgresql://example",
            "TINYFISH_API_KEY": "secret",
        }
        with patch.dict(os.environ, environment, clear=True):
            config = Config()

        self.assertFalse(hasattr(config, "tinyfish_api_key"))
        self.assertNotIn("TINYFISH", dir(config))

    def test_reads_valid_llm_environment(self):
        environment = {
            "OPENCODEX_BASE_URL": "http://proxy:20200/v1",
            "OPENCODEX_API_KEY": "secret",
            "TRANSACTION_LLM_MODEL": "provider/model",
            "TRANSACTION_LLM_TIMEOUT_SECONDS": "4.5",
            "TRANSACTION_LLM_MODE": "write",
        }
        with patch.dict(os.environ, environment, clear=True):
            config = Config()

        self.assertEqual(config.opencodex_base_url, "http://proxy:20200/v1")
        self.assertEqual(config.opencodex_api_key, "secret")
        self.assertEqual(config.transaction_llm_model, "provider/model")
        self.assertEqual(config.transaction_llm_timeout_seconds, 4.5)
        self.assertEqual(config.transaction_llm_mode, "write")

    def test_rejects_invalid_timeout_and_mode(self):
        for environment in (
            {"TRANSACTION_LLM_TIMEOUT_SECONDS": "slow"},
            {"TRANSACTION_LLM_TIMEOUT_SECONDS": "0"},
            {"TRANSACTION_LLM_MODE": "unsafe"},
        ):
            with self.subTest(environment=environment):
                with patch.dict(os.environ, environment, clear=True):
                    with self.assertRaises(ValueError):
                        Config()


class TestTransactionLoaderCliLlm(unittest.TestCase):
    def setUp(self):
        self.cli = TransactionLoaderCLI()

    @staticmethod
    def config(mode="shadow") -> Config:
        return cast(
            Config,
            SimpleNamespace(
                opencodex_base_url="http://localhost:10100",
                opencodex_api_key="x",
                transaction_llm_model="exact-model",
                enriched_transaction_llm_model="enriched-model",
                transaction_llm_timeout_seconds=5.0,
                transaction_llm_mode=mode,
            ),
        )

    def test_categorizer_construction_is_lazy(self):
        categorizer = self.cli._build_categorizer(self.config(), "parents_finance")

        self.assertIsInstance(categorizer, OpenCodexCategorizer)
        self.assertEqual(categorizer.provider_call_count, 0)
        self.assertEqual(categorizer.config.mode, "shadow")

    def test_unapproved_write_mode_aborts_before_any_mutation(self):
        args = argparse.Namespace(
            type="amex",
            filepath="statement.csv",
            folder=None,
            database="finance",
        )
        database = Mock()
        self.cli._validate_arguments = Mock()
        self.cli._build_file_list = Mock(return_value=["statement.csv"])
        self.cli._get_database_instance = Mock(return_value=database)

        with (
            patch(
                "cli.transaction_loader_cli.Config", return_value=self.config("write")
            ),
            patch(
                "cli.transaction_loader_cli.write_authorizers_for",
                side_effect=ApprovalError("approval missing"),
            ),
            patch("cli.transaction_loader_cli.TransactionProcessor") as processor_type,
        ):
            with self.assertRaises(SystemExit) as raised:
                self.cli.run(args)

        self.assertEqual(raised.exception.code, 1)
        processor_type.assert_not_called()
        database.insert_expense.assert_not_called()

    def test_shadow_run_injects_one_categorizer_into_processor(self):
        args = argparse.Namespace(
            type="amex",
            filepath="statement.csv",
            folder=None,
            database="finance",
        )
        database = Mock()
        loader = object()
        results = Mock()
        processor = Mock()
        self.cli._validate_arguments = Mock()
        self.cli._build_file_list = Mock(return_value=["statement.csv"])
        self.cli._get_database_instance = Mock(return_value=database)

        with (
            patch("cli.transaction_loader_cli.Config", return_value=self.config()),
            patch(
                "cli.transaction_loader_cli.write_authorizers_for",
                return_value=WriteAuthorizers(),
            ),
            patch("cli.transaction_loader_cli.TransactionLoader", return_value=loader),
            patch(
                "cli.transaction_loader_cli.TransactionProcessor",
                return_value=processor,
            ) as processor_type,
        ):
            processor.process_files.return_value = results
            results.get_exit_code.return_value = 0
            self.cli.run(args)

        categorizer = processor_type.call_args.args[2]
        self.assertIsInstance(categorizer, OpenCodexCategorizer)
        self.assertEqual(categorizer.provider_call_count, 0)
        processor.process_files.assert_called_once_with("amex", ["statement.csv"])
        results.print_summary.assert_called_once_with(1)


class TestTransactionProcessorLlmInjection(unittest.TestCase):
    def test_unknown_row_receives_injected_categorizer(self):
        database = Mock()
        database.check_if_expense_exists.return_value = False
        database.insert_expense.return_value = TransactionOutcome(
            TransactionStatus.INSERTED, Resolution.LLM
        )
        categorizer = object()
        processor = TransactionProcessor(database, Mock(), categorizer)
        frame = pl.DataFrame(
            {
                "date": ["2026-09-15"],
                "merchant": ["OPENAI"],
                "cost": [20.0],
                "cc_category": ["Services"],
            }
        )

        totals, unresolved_rows, suggestion_ids = processor._insert_transactions(
            frame, "amex"
        )

        self.assertEqual(totals[TransactionStatus.INSERTED], 1)
        self.assertEqual(unresolved_rows, [])
        self.assertEqual(suggestion_ids, [])
        self.assertIs(
            database.insert_expense.call_args.kwargs["categorizer"], categorizer
        )

    def test_duplicate_row_never_reaches_insertion_or_categorizer(self):
        database = Mock()
        database.check_if_expense_exists.return_value = True
        categorizer = object()
        processor = TransactionProcessor(database, Mock(), categorizer)
        frame = pl.DataFrame(
            {
                "date": ["2026-09-15"],
                "merchant": ["OPENAI"],
                "cost": [20.0],
                "cc_category": ["Services"],
            }
        )

        totals, unresolved_rows, suggestion_ids = processor._insert_transactions(
            frame, "amex"
        )

        self.assertEqual(totals[TransactionStatus.DUPLICATE], 1)
        self.assertEqual(totals[TransactionStatus.INSERTED], 0)
        self.assertEqual(unresolved_rows, [])
        self.assertEqual(suggestion_ids, [])
        database.insert_expense.assert_not_called()


if __name__ == "__main__":
    unittest.main()

"""Tests for T12 protocol-specific model configuration and research docs.

Pins V51 (enriched ``finance`` reads ``ENRICHED_TRANSACTION_LLM_MODEL`` while
``TRANSACTION_LLM_MODEL`` and ``parents_finance`` stay untouched) and V39/V48
(only the research entry point resolves ``TINYFISH_API_KEY``), plus the
documented research limits from ``I.TinyFishResearchConfig``.
"""

from __future__ import annotations

import os
import unittest
from pathlib import Path
from types import SimpleNamespace
from typing import cast
from unittest.mock import patch

from config import Config
from services import categorizer_factory as factory_module
from services.categorizer_factory import build_categorizer
from services.tinyfish_research import (
    DEFAULT_FETCH_TIMEOUT_SECONDS,
    DEFAULT_SEARCH_TIMEOUT_SECONDS,
    MAX_REQUESTS_PER_MINUTE,
    TINYFISH_API_KEY_ENV,
)

REPO_ROOT = Path(__file__).resolve().parents[1]
LEGACY_MODEL = "legacy/model"
ENRICHED_MODEL = "enriched/model"


def fake_config(mode: str = "write") -> Config:
    return cast(
        Config,
        SimpleNamespace(
            opencodex_base_url="http://localhost:10100",
            opencodex_api_key="x",
            transaction_llm_model=LEGACY_MODEL,
            enriched_transaction_llm_model=ENRICHED_MODEL,
            transaction_llm_timeout_seconds=5.0,
            transaction_llm_mode=mode,
        ),
    )


class TestConfigModelDefaults(unittest.TestCase):
    def test_enriched_default_is_haiku_and_legacy_default_is_untouched(self):
        with patch.dict(
            os.environ, {"POSTGRES_CONNECTION_STRING": "postgresql://x"}, clear=True
        ):
            config = Config()

        self.assertEqual(
            config.enriched_transaction_llm_model, "anthropic/claude-haiku-4-5"
        )
        self.assertEqual(
            config.transaction_llm_model,
            "SingularityApiDev/deepseek-v4-flash-0731",
        )

    def test_enriched_override_does_not_move_the_legacy_model(self):
        environment = {
            "TRANSACTION_LLM_MODEL": LEGACY_MODEL,
            "ENRICHED_TRANSACTION_LLM_MODEL": ENRICHED_MODEL,
        }
        with patch.dict(os.environ, environment, clear=True):
            config = Config()

        self.assertEqual(config.transaction_llm_model, LEGACY_MODEL)
        self.assertEqual(config.enriched_transaction_llm_model, ENRICHED_MODEL)

    def test_config_construction_never_reads_the_tinyfish_key(self):
        environment = {
            "POSTGRES_CONNECTION_STRING": "postgresql://x",
            "TINYFISH_API_KEY": "secret",
        }
        with patch.dict(os.environ, environment, clear=True):
            config = Config()

        self.assertFalse(hasattr(config, "tinyfish_api_key"))

    def test_config_source_never_mentions_tinyfish(self):
        source = (REPO_ROOT / "config.py").read_text(encoding="utf-8")

        self.assertNotIn("TINYFISH", source)


class TestResearchClientLimits(unittest.TestCase):
    """The researched limits are already correct; T12 must not drift them."""

    def test_search_timeout_is_thirty_seconds(self):
        self.assertEqual(DEFAULT_SEARCH_TIMEOUT_SECONDS, 30.0)

    def test_fetch_timeout_is_one_hundred_fifty_seconds(self):
        self.assertEqual(DEFAULT_FETCH_TIMEOUT_SECONDS, 150.0)

    def test_pacing_targets_thirty_requests_per_minute(self):
        self.assertEqual(MAX_REQUESTS_PER_MINUTE, 30)

    def test_the_key_environment_name_is_documented(self):
        self.assertEqual(TINYFISH_API_KEY_ENV, "TINYFISH_API_KEY")


class TestModelSeam(unittest.TestCase):
    def test_omitting_the_model_keeps_the_unenriched_default(self):
        categorizer = build_categorizer(fake_config())

        self.assertEqual(categorizer.config.model, LEGACY_MODEL)

    def test_an_explicit_model_wins(self):
        categorizer = build_categorizer(fake_config(), model=ENRICHED_MODEL)

        self.assertEqual(categorizer.config.model, ENRICHED_MODEL)


class TestAuthorizerModelBinding(unittest.TestCase):
    """Approval fingerprints must bind the exact model the path calls (V51)."""

    def test_enriched_authorizer_binds_the_enriched_model(self):
        with patch.object(factory_module, "authorize_enriched_write_mode") as authorize:
            authorize.return_value = lambda *args: True

            factory_module.enriched_write_authorizer_for(
                fake_config(),
                "finance",
                lambda: [],
                packet_resolver=lambda merchant: None,
            )

        self.assertEqual(authorize.call_args.kwargs["model"], ENRICHED_MODEL)

    def test_legacy_authorizer_binds_the_unenriched_model(self):
        with patch.object(factory_module, "authorize_write_mode") as authorize:
            authorize.return_value = lambda *args: True

            factory_module.write_authorizer_for(
                fake_config(), "parents_finance", lambda: []
            )

        self.assertEqual(authorize.call_args.kwargs["model"], LEGACY_MODEL)

    def test_routing_sends_each_database_to_its_own_protocol(self):
        with (
            patch.object(factory_module, "authorize_enriched_write_mode") as enriched,
            patch.object(factory_module, "authorize_write_mode") as legacy,
        ):
            enriched.return_value = lambda *args: True
            legacy.return_value = lambda *args: True

            finance = factory_module.write_authorizers_for(
                fake_config(),
                "finance",
                lambda: [],
                packet_resolver=lambda merchant: None,
            )
            parents = factory_module.write_authorizers_for(
                fake_config(),
                "parents_finance",
                lambda: [],
                packet_resolver=lambda merchant: None,
            )

        self.assertIsNotNone(finance.enriched)
        self.assertIsNone(finance.legacy)
        self.assertIsNotNone(parents.legacy)
        self.assertIsNone(parents.enriched)
        self.assertEqual(enriched.call_args.kwargs["model"], ENRICHED_MODEL)
        self.assertEqual(legacy.call_args.kwargs["model"], LEGACY_MODEL)

    def test_shadow_mode_never_reads_the_taxonomy(self):
        calls = []

        def provider():
            calls.append(1)
            return []

        with patch.object(factory_module, "authorize_enriched_write_mode") as authorize:
            result = factory_module.enriched_write_authorizer_for(
                fake_config(mode="shadow"),
                "finance",
                provider,
                packet_resolver=lambda merchant: None,
            )

        self.assertIsNone(result)
        self.assertEqual(calls, [])
        authorize.assert_not_called()


if __name__ == "__main__":
    unittest.main()

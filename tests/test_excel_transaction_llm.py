import importlib.util
import io
import unittest
from contextlib import redirect_stdout
from datetime import date
from pathlib import Path
from unittest.mock import patch

import polars as pl

from services.transaction_categorization import (
    Resolution,
    TransactionOutcome,
    TransactionStatus,
    UnresolvedReason,
)
from services.transaction_llm_approval import ApprovalError


REPO_ROOT = Path(__file__).resolve().parents[1]
MODULE_PATH = REPO_ROOT / "load-excel-transactions.py"


def load_excel_module():
    spec = importlib.util.spec_from_file_location(
        "load_excel_transactions", MODULE_PATH
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


excel = load_excel_module()


def make_frame(rows):
    return pl.DataFrame(
        rows,
        schema={
            "DATE": pl.Date,
            "DETAILSDescriptions": pl.Utf8,
            "DR_PAYMENTs": pl.Float64,
            "ACCT_subCODE": pl.Utf8,
            "ACCT_CODE": pl.Utf8,
        },
        orient="row",
    )


def row(merchant="HYDRO ONE", cost=41.5, sub_code="REG", code="Utilities"):
    return (date(2026, 9, 15), merchant, cost, sub_code, code)


class FakeParentsDB:
    def __init__(self, outcomes=None, existing=False, **_):
        self.outcomes = list(outcomes or [])
        self.existing = existing
        self.insert_calls = []
        self.deleted_ids = []

    def check_if_expense_exists(self, date, merchant, cost):
        return self.existing

    def get_categorization_choices(self):
        return [{"category_id": 1, "category_name": "Utilities"}]

    def get_expense_id(self, date, merchant, cost):
        return 4242

    def delete_expense(self, expense_id):
        self.deleted_ids.append(expense_id)

    def insert_expense(self, date, merchant, cost, cc_category=None, categorizer=None):
        self.insert_calls.append(
            {
                "date": date,
                "merchant": merchant,
                "cost": cost,
                "cc_category": cc_category,
                "categorizer": categorizer,
            }
        )
        if self.outcomes:
            return self.outcomes.pop(0)
        return TransactionOutcome(TransactionStatus.INSERTED, Resolution.DETERMINISTIC)


class ExcelRunTestCase(unittest.TestCase):
    def setUp(self):
        self.categorizer = object()
        self.messages = []

    def run_excel(
        self,
        rows,
        outcomes=None,
        existing=False,
        cron=False,
        file_path="/tmp/tdvisa.xlsx",
        write_mode_error=None,
    ):
        self.db = FakeParentsDB(outcomes=outcomes, existing=existing)
        constructed = []
        self.constructed_dbs = constructed

        def make_db(**kwargs):
            constructed.append(kwargs)
            return self.db

        def fake_authorizer(_config, _database, _choices):
            if write_mode_error is not None:
                raise write_mode_error
            return None

        with (
            patch.object(excel.pl, "read_excel", return_value=make_frame(rows)),
            patch.object(excel, "Config", return_value=object()),
            patch.object(excel, "write_authorizer_for", side_effect=fake_authorizer),
            patch.object(excel, "build_categorizer", return_value=self.categorizer),
            patch.object(excel, "ParentsFinanceDB", side_effect=make_db),
            patch.object(
                excel, "send_discord_message", side_effect=self.messages.append
            ),
            patch("builtins.input", side_effect=AssertionError("prompted")),
        ):
            totals = excel.run(file_path, cron, file_path)

        return totals


class TestExcelCategorizerWiring(ExcelRunTestCase):
    def test_statement_category_passed_by_keyword_with_categorizer(self):
        totals = self.run_excel([row()])

        call = self.db.insert_calls[0]
        self.assertEqual(call["cc_category"], "Utilities")
        self.assertIs(call["categorizer"], self.categorizer)
        self.assertEqual(totals[TransactionStatus.INSERTED], 1)

    def test_cron_mode_also_injects_categorizer(self):
        self.run_excel([row()], cron=True)

        self.assertIs(self.db.insert_calls[0]["categorizer"], self.categorizer)

    def test_unapproved_write_mode_aborts_before_any_mutation(self):
        with self.assertRaises(RuntimeError):
            self.run_excel(
                [row()],
                write_mode_error=RuntimeError("approval required"),
            )

        # The database object is built read-only; no row may be written.
        self.assertEqual(self.db.insert_calls, [])
        self.assertEqual(self.db.deleted_ids, [])
        self.assertEqual(self.messages, [])


class TestExcelSkipAndDeleteOrdering(ExcelRunTestCase):
    def test_transfer_marker_with_existing_row_deletes_before_categorization(self):
        totals = self.run_excel([row(sub_code="Tfr=123")], existing=True)

        self.assertEqual(self.db.deleted_ids, [4242])
        self.assertEqual(self.db.insert_calls, [])
        self.assertEqual(totals[TransactionStatus.DELETED], 1)

    def test_transfer_marker_without_existing_row_is_ignored(self):
        totals = self.run_excel([row(sub_code="TFR-TO 999")], existing=False)

        self.assertEqual(self.db.deleted_ids, [])
        self.assertEqual(self.db.insert_calls, [])
        self.assertEqual(totals[TransactionStatus.IGNORED], 1)

    def test_chequing_keyword_only_skips_in_chequing_files(self):
        cheque_totals = self.run_excel(
            [row(sub_code="Tfr-out")], file_path="/tmp/tdcheq_sept.xlsx"
        )
        self.assertEqual(cheque_totals[TransactionStatus.IGNORED], 1)
        self.assertEqual(self.db.insert_calls, [])

        visa_totals = self.run_excel(
            [row(sub_code="Tfr-out")], file_path="/tmp/tdvisa.xlsx"
        )
        self.assertEqual(visa_totals[TransactionStatus.INSERTED], 1)
        self.assertEqual(len(self.db.insert_calls), 1)

    def test_merchant_skip_keyword_is_ignored_without_categorization(self):
        totals = self.run_excel([row(merchant="LOAN PAYMENT 55")])

        self.assertEqual(self.db.insert_calls, [])
        self.assertEqual(totals[TransactionStatus.IGNORED], 1)


class TestExcelOutcomeTotals(ExcelRunTestCase):
    def test_every_processed_row_contributes_exactly_one_outcome(self):
        rows = [
            row(sub_code="Tfr=1"),
            row(merchant="IG FIN SER SFGI INV"),
            row(merchant="OPENAI"),
            row(merchant="UNKNOWN CO"),
            row(merchant="DUPE CO"),
        ]
        outcomes = [
            TransactionOutcome(TransactionStatus.INSERTED, Resolution.DETERMINISTIC),
            TransactionOutcome(
                TransactionStatus.SHADOW, Resolution.LLM, suggested_choice_id=2
            ),
            TransactionOutcome(TransactionStatus.DUPLICATE),
        ]

        totals = self.run_excel(rows, outcomes=outcomes)

        self.assertEqual(sum(totals.values()), len(rows))
        self.assertEqual(totals[TransactionStatus.IGNORED], 2)
        self.assertEqual(totals[TransactionStatus.INSERTED], 1)
        self.assertEqual(totals[TransactionStatus.SHADOW], 1)
        self.assertEqual(totals[TransactionStatus.DUPLICATE], 1)

    def test_unresolved_row_does_not_stop_later_rows(self):
        rows = [row(merchant="FIRST CO"), row(merchant="SECOND CO")]
        outcomes = [
            TransactionOutcome(
                TransactionStatus.UNRESOLVED,
                Resolution.NONE,
                UnresolvedReason.TIMEOUT,
            ),
            TransactionOutcome(TransactionStatus.INSERTED, Resolution.LLM),
        ]

        totals = self.run_excel(rows, outcomes=outcomes)

        self.assertEqual(len(self.db.insert_calls), 2)
        self.assertEqual(totals[TransactionStatus.UNRESOLVED], 1)
        self.assertEqual(totals[TransactionStatus.INSERTED], 1)


class TestExcelStatusReporting(ExcelRunTestCase):
    def test_cron_notification_reports_status_and_all_counts(self):
        outcomes = [
            TransactionOutcome(
                TransactionStatus.SHADOW, Resolution.LLM, suggested_choice_id=1
            )
        ]

        self.run_excel([row()], outcomes=outcomes, cron=True)

        self.assertEqual(len(self.messages), 1)
        message = self.messages[0]
        self.assertIn("Run partial", message)
        for status in TransactionStatus:
            self.assertIn(f"{status.value}=", message)

    def test_complete_status_when_no_shadow_or_unresolved_rows(self):
        self.run_excel([row()], cron=True)

        self.assertIn("Run complete", self.messages[0])

    def test_run_status_helper_flags_unresolved_as_partial(self):
        totals = {status: 0 for status in TransactionStatus}
        totals[TransactionStatus.INSERTED] = 3
        self.assertEqual(excel.run_status(totals), "complete")

        totals[TransactionStatus.UNRESOLVED] = 1
        self.assertEqual(excel.run_status(totals), "partial")

    def test_cron_output_prints_no_merchant_detail(self):
        outcomes = [
            TransactionOutcome(
                TransactionStatus.UNRESOLVED, Resolution.LLM, UnresolvedReason.ABSTAINED
            )
        ]
        captured = io.StringIO()

        with redirect_stdout(captured):
            self.run_excel(
                [row(merchant="SECRET MERCHANT")], outcomes=outcomes, cron=True
            )

        # V35: cron/persistent output carries the reason, never the merchant.
        self.assertNotIn("SECRET MERCHANT", captured.getvalue())
        self.assertIn("abstained", captured.getvalue())

    def test_local_output_may_print_merchant_detail(self):
        outcomes = [
            TransactionOutcome(
                TransactionStatus.UNRESOLVED, Resolution.LLM, UnresolvedReason.ABSTAINED
            )
        ]
        captured = io.StringIO()

        with redirect_stdout(captured):
            self.run_excel(
                [row(merchant="LOCAL MERCHANT")], outcomes=outcomes, cron=False
            )

        self.assertIn("LOCAL MERCHANT", captured.getvalue())

    def test_cron_summary_reports_suggestion_ids_only(self):
        outcomes = [
            TransactionOutcome(
                TransactionStatus.SUGGESTED,
                Resolution.LLM,
                suggestion_id="deadbeefdeadbeef",
            )
        ]

        self.run_excel([row()], outcomes=outcomes, cron=True)

        summary = self.messages[0]
        # V35: counts plus identifiers, never the proposal or its citations.
        self.assertIn("suggested=1", summary)
        self.assertIn("deadbeefdeadbeef", summary)

    def test_suggested_row_makes_the_run_partial(self):
        outcomes = [
            TransactionOutcome(
                TransactionStatus.SUGGESTED,
                Resolution.LLM,
                suggestion_id="deadbeefdeadbeef",
            )
        ]

        totals = self.run_excel([row()], outcomes=outcomes, cron=True)

        # V40: a suggested row is unfinished, so the run is never complete.
        self.assertEqual(excel.run_status(totals), "partial")


class TestMainExitContract(unittest.TestCase):
    """V37/V39: exit 0 for complete/partial, 1 on file error or approval abort."""

    def test_main_returns_zero_when_run_completes(self):
        totals = {status: 0 for status in TransactionStatus}
        totals[TransactionStatus.INSERTED] = 2

        with patch.object(excel, "run", return_value=totals) as mock_run:
            code = excel.main(["--filepath", "book.xlsx"])

        self.assertEqual(code, 0)
        mock_run.assert_called_once_with("book.xlsx", False, "book.xlsx")

    def test_main_returns_zero_on_partial_run(self):
        totals = {status: 0 for status in TransactionStatus}
        totals[TransactionStatus.SHADOW] = 2
        totals[TransactionStatus.UNRESOLVED] = 1

        with patch.object(excel, "run", return_value=totals):
            code = excel.main(["--filepath", "book.xlsx"])

        self.assertEqual(code, 0)

    def test_main_returns_one_on_file_error(self):
        with patch.object(excel, "run", side_effect=RuntimeError("bad excel")):
            code = excel.main(["--filepath", "missing.xlsx"])

        self.assertEqual(code, 1)

    def test_main_returns_one_on_approval_abort(self):
        with patch.object(excel, "run", side_effect=ApprovalError("approval missing")):
            code = excel.main(["--filepath", "book.xlsx"])

        self.assertEqual(code, 1)

    def test_main_cron_error_notifies_discord_and_returns_one(self):
        messages = []
        with (
            patch.object(excel, "run", side_effect=RuntimeError("bad excel")),
            patch.object(excel, "send_discord_message", messages.append),
        ):
            code = excel.main(["--filepath", "missing.xlsx", "--cron", "1"])

        self.assertEqual(code, 1)
        self.assertEqual(len(messages), 1)
        self.assertIn("missing.xlsx", messages[0])

    def test_main_returns_one_on_keyboard_interrupt(self):
        with patch.object(excel, "run", side_effect=KeyboardInterrupt):
            code = excel.main(["--filepath", "book.xlsx"])

        self.assertEqual(code, 1)

    def test_main_removes_temporary_ftp_download_on_failure(self):
        removed = []
        with (
            patch.object(excel, "fetch_ftp_file", return_value="/tmp/dl.xlsx"),
            patch.object(excel, "run", side_effect=RuntimeError("bad excel")),
            patch.object(excel.os, "remove", removed.append),
        ):
            code = excel.main(["--filepath", "ftp://host/book.xlsx"])

        self.assertEqual(code, 1)
        self.assertEqual(removed, ["/tmp/dl.xlsx"])


class TestExplicitImports(unittest.TestCase):
    """V41: modules import the exact submodule they use."""

    def test_urllib_request_imported_explicitly(self):
        source = MODULE_PATH.read_text(encoding="utf-8")
        self.assertIn("import urllib.request", source)


if __name__ == "__main__":
    unittest.main()

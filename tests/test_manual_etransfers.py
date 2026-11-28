"""Offline tests for statement-level manual e-transfer categorization."""

import contextlib
import importlib.util
import io
import json
import tempfile
import unittest
from datetime import date
from pathlib import Path
from unittest.mock import patch

from db.parents_finance import ParentsFinanceDB
from services.manual_etransfers import needs_manual_context, statement_transfers
from services.transaction_categorization import (
    Resolution,
    TransactionOutcome,
    TransactionStatus,
    normalize_context_text,
)

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location(
    "manual_transfer_cli", ROOT / "categorize-etransfers.py"
)
cli = importlib.util.module_from_spec(spec)
spec.loader.exec_module(cli)

CHOICES = [
    {
        "category_id": 1,
        "subcategory_id": 11,
        "category_name": "Food",
        "subcategory_name": "Grocery",
    },
    {
        "category_id": 2,
        "subcategory_id": 22,
        "category_name": "Home",
        "subcategory_name": "Rent",
    },
]


def row(day=1, merchant="Interac e-Transfer: Example", cost=20.0):
    return {"date": date(2026, 10, day), "merchant": merchant, "cost": cost}


class FakeDatabase:
    def __init__(self):
        self.saved = []
        self.existing = set()

    def get_categorization_choices(self):
        return CHOICES

    def check_if_expense_exists(self, day, merchant, cost):
        return (day, merchant, cost) in self.existing

    def insert_manual_etransfer(self, day, merchant, cost, choice_id):
        if choice_id not in {11, 22}:
            raise ValueError("Unknown subcategory_id")
        if self.check_if_expense_exists(day, merchant, cost):
            return TransactionOutcome(TransactionStatus.DUPLICATE)
        self.saved.append((day, merchant, cost, choice_id))
        self.existing.add((day, merchant, cost))
        return TransactionOutcome(TransactionStatus.INSERTED)


class TestStatementTransfers(unittest.TestCase):
    def test_same_recipient_keeps_distinct_dates_and_amounts(self):
        transfers = statement_transfers(
            [row(), row(), row(2), row(cost=30), row(merchant="SHOP")]
        )
        self.assertEqual(len(transfers), 3)
        self.assertEqual(len({item.transaction_id for item in transfers}), 3)

    def test_normalized_prefix_and_incoming_amount(self):
        (transfer,) = statement_transfers(
            [row(merchant="INTERAC  E-TRANSFER: Example", cost=-20)]
        )
        self.assertEqual(transfer.as_dict()["cost"], "-20.00")

    def test_invalid_money_and_date_fail(self):
        for bad in [row(cost=0.005), row(cost=float("nan")), {**row(), "date": None}]:
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                statement_transfers([bad])


class TestManualContextByDatabase(unittest.TestCase):
    def test_parents_markers_are_held(self):
        for descriptor in (
            "Interac e-Transfer: Example",
            "SEND E-TFR ***abc",
            "E-TRANSFER 123",
            "ABC TFR-TO 99",
            "ABC TFR-FR 99",
            "TFR TO savings",
            "CASH WITHDRAWAL",
            "CASH WITHDRA",
            "CAD DRAFT JANE EXAMPLE",
            "DRAFT # 5",
            "CHQ#00012-3",
            "TD ATM W/D 004",
            "W/DCASH_ 1",
        ):
            with self.subTest(descriptor=descriptor):
                self.assertTrue(
                    needs_manual_context(
                        normalize_context_text(descriptor), "parents_finance"
                    )
                )

    def test_ordinary_merchants_are_not_held(self):
        for descriptor in ("XCHQMART", "NEW/DEAL SHOP", "DRAFTHOUSE CINEMA"):
            with self.subTest(descriptor=descriptor):
                self.assertFalse(
                    needs_manual_context(
                        normalize_context_text(descriptor), "parents_finance"
                    )
                )

    def test_finance_default_is_unchanged(self):
        self.assertFalse(needs_manual_context("cad draft jane example"))
        self.assertTrue(needs_manual_context("interac e-transfer: x"))

    def test_statement_transfers_uses_the_database_rule(self):
        rows = [row(merchant="CHQ#1"), row(merchant="SHOP")]

        self.assertEqual(statement_transfers(rows), ())
        self.assertEqual(
            [t.merchant for t in statement_transfers(rows, "parents_finance")],
            ["CHQ#1"],
        )


class TestManualTransferCLI(unittest.TestCase):
    def setUp(self):
        self.db = FakeDatabase()
        self.transfers = statement_transfers([row(), row(2)])

    def run_cli(self, extra=(), answers=()):
        output = io.StringIO()
        with (
            patch.object(cli, "load_transfers", return_value=self.transfers),
            patch.object(cli, "MyFinanceDB", return_value=self.db),
            patch.object(cli.sys.stdin, "isatty", return_value=True),
            patch("builtins.input", side_effect=answers),
            contextlib.redirect_stdout(output),
            contextlib.redirect_stderr(io.StringIO()),
        ):
            code = cli.main(["--filepath", "example.csv", *extra])
        return code, output.getvalue()

    def test_agent_lists_choices_and_pending_transactions_without_writes(self):
        first = self.transfers[0]
        self.db.existing.add((first.date, first.merchant, first.cost))
        code, output = self.run_cli(["--list"])
        payload = json.loads(output)
        self.assertEqual(code, 0)
        self.assertEqual(payload["choices"], CHOICES)
        self.assertEqual(payload["transactions"], [self.transfers[1].as_dict()])
        self.assertEqual(self.db.saved, [])

    def test_agent_saves_only_named_transaction_then_can_resume(self):
        transaction_id = self.transfers[0].transaction_id
        for expected in ["inserted", "duplicate"]:
            code, output = self.run_cli(
                ["--transaction", transaction_id, "--subcategory", "22"]
            )
            self.assertEqual(code, 0)
            self.assertEqual(json.loads(output)["status"], expected)
        self.assertEqual(len(self.db.saved), 1)
        _, output = self.run_cli(["--list"])
        self.assertEqual(len(json.loads(output)["transactions"]), 1)

    def test_unknown_transaction_or_choice_never_writes(self):
        for transaction_id, choice in [
            ("bad", "11"),
            (self.transfers[0].transaction_id, "999"),
        ]:
            with self.subTest(transaction_id=transaction_id, choice=choice):
                code, _ = self.run_cli(
                    ["--transaction", transaction_id, "--subcategory", choice]
                )
                self.assertEqual(code, 1)
        self.assertEqual(self.db.saved, [])

    def test_interactive_retries_invalid_input_then_saves_different_choices(self):
        code, output = self.run_cli(answers=["bad", "999", "11", "22"])
        self.assertEqual(code, 0)
        self.assertIn("Unknown subcategory", output)
        self.assertEqual([item[-1] for item in self.db.saved], [11, 22])

    def test_skip_quit_and_interrupt_leave_transactions_pending(self):
        for answers in [["s", "q"], [EOFError()], [KeyboardInterrupt()]]:
            with self.subTest(answers=answers):
                code, _ = self.run_cli(answers=answers)
                self.assertEqual(code, 0)
                self.assertEqual(self.db.saved, [])

    def test_real_statement_loader_keeps_json_clean_and_existing_filters(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "transfers.csv"
            path.write_text(
                "description,type,amount,date\n"
                "Example,Interac e-Transfer,-$20.00,2026-10-01\n"
                "Nathan Li Simplii,Interac e-Transfer,-$20.00,2026-10-01\n"
                "SHOP,Purchase,-$5.00,2026-10-01\n"
            )
            output = io.StringIO()
            with (
                patch.object(cli, "MyFinanceDB", return_value=self.db),
                contextlib.redirect_stdout(output),
                contextlib.redirect_stderr(io.StringIO()),
            ):
                code = cli.main(["--filepath", str(path), "--list"])
            self.assertEqual(code, 0)
            payload = json.loads(output.getvalue())
            self.assertEqual(len(payload["transactions"]), 1)
            self.assertEqual(payload["transactions"][0]["cost"], "20.00")

    def test_noninteractive_default_and_incomplete_answer_fail_before_loading(self):
        for extra in [[], ["--transaction", "id"], ["--list", "--subcategory", "11"]]:
            with (
                self.subTest(extra=extra),
                patch.object(cli.sys.stdin, "isatty", return_value=False),
                patch.object(cli, "load_transfers") as load,
                contextlib.redirect_stderr(io.StringIO()),
            ):
                with self.assertRaises(SystemExit) as raised:
                    cli.main(["--filepath", "example.csv", *extra])
                self.assertEqual(raised.exception.code, 2)
                load.assert_not_called()


PARENTS_CHOICES = [
    {"category_id": 1, "category_name": "Groceries"},
    {"category_id": 2, "category_name": "Cash"},
]


class FakeParentsDB(ParentsFinanceDB):
    """Real insert logic; no connection."""

    def __init__(self):
        self.inserts = []
        self.existing = set()

    def get_categorization_choices(self):
        return PARENTS_CHOICES

    def check_if_expense_exists(self, day, merchant, cost):
        return (day, merchant, cost) in self.existing

    def insert(self, query, args):
        self.inserts.append((query, args))
        self.existing.add(args[:3])


class TestParentsManualInsert(unittest.TestCase):
    day = date(2026, 10, 1)

    def test_inserts_one_expense_and_no_rule(self):
        database = FakeParentsDB()

        outcome = database.insert_manual_transfer(self.day, "CHQ#1", 20, 2)
        repeat = database.insert_manual_transfer(self.day, "CHQ#1", 20, 2)

        self.assertEqual(outcome.status, TransactionStatus.INSERTED)
        self.assertEqual(outcome.resolution, Resolution.MANUAL)
        self.assertEqual(repeat.status, TransactionStatus.DUPLICATE)
        ((query, args),) = database.inserts
        self.assertIn("insert into expenses", query)
        self.assertEqual(args, (self.day, "CHQ#1", 20, 2))

    def test_refuses_unheld_rows_and_unknown_categories(self):
        database = FakeParentsDB()
        for merchant, category_id in (("SHOP", 1), ("CHQ#1", 99), ("CHQ#1", True)):
            with self.subTest(merchant=merchant, category_id=category_id):
                with self.assertRaises(ValueError):
                    database.insert_manual_transfer(self.day, merchant, 20, category_id)
        self.assertEqual(database.inserts, [])


class TestParentsManualCLI(unittest.TestCase):
    def setUp(self):
        self.db = FakeParentsDB()
        self.transfers = statement_transfers(
            [row(merchant="CHQ#1"), row(day=2, merchant="TFR TO savings")],
            "parents_finance",
        )

    def run_cli(self, extra, answers=()):
        output = io.StringIO()
        with (
            patch.object(cli, "ParentsFinanceDB", return_value=self.db),
            patch.object(cli, "MyFinanceDB", side_effect=AssertionError("finance")),
            patch.object(cli, "load_transfers", return_value=self.transfers),
            patch.object(cli.sys.stdin, "isatty", return_value=True),
            patch("builtins.input", side_effect=list(answers)),
            contextlib.redirect_stdout(output),
            contextlib.redirect_stderr(io.StringIO()),
        ):
            code = cli.main(
                ["--database", "parents_finance", "--filepath", "x.csv", *extra]
            )
        return code, output.getvalue()

    def test_list_then_save_one_by_category(self):
        code, output = self.run_cli(["--list"])
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(output)["choices"], PARENTS_CHOICES)
        self.assertEqual(self.db.inserts, [])

        code, output = self.run_cli(
            ["--transaction", self.transfers[0].transaction_id, "--category", "1"]
        )

        self.assertEqual((code, json.loads(output)["status"]), (0, "inserted"))
        self.assertEqual(len(self.db.inserts), 1)

    def test_interactive_asks_for_category_ids(self):
        code, output = self.run_cli([], answers=["99", "2", "s"])

        self.assertEqual(code, 0)
        self.assertIn("2: Cash", output)
        self.assertIn("Unknown category ID", output)
        self.assertEqual([args[-1] for _, args in self.db.inserts], [2])

    def test_each_database_accepts_only_its_own_choice_flag(self):
        for argv in (
            [
                "--database",
                "parents_finance",
                "--transaction",
                "a",
                "--subcategory",
                "1",
            ],
            ["--database", "parents_finance", "--list", "--subcategory", "1"],
            ["--transaction", "a", "--category", "1"],
            ["--list", "--category", "1"],
        ):
            with (
                self.subTest(argv=argv),
                patch.object(cli, "load_transfers") as load,
                contextlib.redirect_stderr(io.StringIO()),
            ):
                with self.assertRaises(SystemExit) as raised:
                    cli.main(["--filepath", "x.csv", *argv])
                self.assertEqual(raised.exception.code, 2)
                load.assert_not_called()

    def test_statements_are_filtered_by_the_parents_rule(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "td.csv"
            path.write_text(
                "2026-10-01,CHQ#00012,20.00,,100.00\n2026-10-01,SHOP,5.00,,95.00\n"
            )
            output = io.StringIO()
            with (
                patch.object(cli, "ParentsFinanceDB", return_value=self.db),
                contextlib.redirect_stdout(output),
                contextlib.redirect_stderr(io.StringIO()),
            ):
                code = cli.main(
                    [
                        "--database",
                        "parents_finance",
                        "--type",
                        "td_debit",
                        "--filepath",
                        str(path),
                        "--list",
                    ]
                )

        self.assertEqual(code, 0)
        self.assertEqual(
            [t["merchant"] for t in json.loads(output.getvalue())["transactions"]],
            ["CHQ#00012"],
        )


if __name__ == "__main__":
    unittest.main()

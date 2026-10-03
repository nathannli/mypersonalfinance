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

from services.manual_etransfers import statement_transfers
from services.transaction_categorization import TransactionOutcome, TransactionStatus

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("manual_transfer_cli", ROOT / "categorize-etransfers.py")
cli = importlib.util.module_from_spec(spec)
spec.loader.exec_module(cli)

CHOICES = [
    {"category_id": 1, "subcategory_id": 11, "category_name": "Food", "subcategory_name": "Grocery"},
    {"category_id": 2, "subcategory_id": 22, "category_name": "Home", "subcategory_name": "Rent"},
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
        transfers = statement_transfers([row(), row(), row(2), row(cost=30), row(merchant="SHOP")])
        self.assertEqual(len(transfers), 3)
        self.assertEqual(len({item.transaction_id for item in transfers}), 3)

    def test_normalized_prefix_and_incoming_amount(self):
        transfer, = statement_transfers([row(merchant="INTERAC  E-TRANSFER: Example", cost=-20)])
        self.assertEqual(transfer.as_dict()["cost"], "-20.00")

    def test_invalid_money_and_date_fail(self):
        for bad in [row(cost=0.005), row(cost=float("nan")), {**row(), "date": None}]:
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                statement_transfers([bad])


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
            code, output = self.run_cli(["--transaction", transaction_id, "--subcategory", "22"])
            self.assertEqual(code, 0)
            self.assertEqual(json.loads(output)["status"], expected)
        self.assertEqual(len(self.db.saved), 1)
        _, output = self.run_cli(["--list"])
        self.assertEqual(len(json.loads(output)["transactions"]), 1)

    def test_unknown_transaction_or_choice_never_writes(self):
        for transaction_id, choice in [("bad", "11"), (self.transfers[0].transaction_id, "999")]:
            with self.subTest(transaction_id=transaction_id, choice=choice):
                code, _ = self.run_cli(["--transaction", transaction_id, "--subcategory", choice])
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


if __name__ == "__main__":
    unittest.main()

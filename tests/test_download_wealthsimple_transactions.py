import os
import tempfile
import unittest
from datetime import date
from pathlib import Path
from unittest.mock import patch

from scripts import download_wealthsimple_transactions as downloader
from sources.api.wealthsimple_credit import WealthsimpleCreditStatement
from sources.api.wealthsimple_debit import WealthsimpleDebitStatement

HEADER = "description,type,amount,date\n"


def csv_file(directory, body):
    path = Path(directory) / "ws.csv"
    path.write_text(HEADER + body, encoding="utf-8")
    return str(path)


class TestDebitParser(unittest.TestCase):
    def test_filters_and_signs_like_legacy(self):
        with tempfile.TemporaryDirectory() as directory:
            df = WealthsimpleDebitStatement(
                csv_file(
                    directory,
                    "GOODLIFE CLUBS,Pre-authorized debit,− $50.00 CAD,2026-09-16\n"
                    'MICHAEL C REID,Interac e-Transfer,"$1,200.00 CAD",2026-08-24\n'
                    "AMEX,Pre-authorized debit,− $9.00 CAD,2026-09-18\n"
                    "Transfer out,Chequing • Hot,− $3.00 CAD,2026-09-28\n"
                    "Interest,Chequing • Hot,$0.10 CAD,2026-09-01\n"
                    "Sabio Canada In,Direct deposit,$10.00 CAD,2026-09-28\n"
                    "Nathan Li Simplii,Interac e-Transfer,− $4.00 CAD,2026-09-20\n"
                    "Wealthsimple credit card,Credit card payment,− $7.00 CAD,2026-09-08\n",
                )
            ).get_df()

        self.assertEqual(df.columns, ["date", "merchant", "cost", "cc_category"])
        self.assertEqual(
            df.select("merchant", "cost").rows(),
            [
                ("Pre-authorized debit: GOODLIFE CLUBS", 50.0),
                ("Interac e-Transfer: MICHAEL C REID", -1200.0),
            ],
        )
        self.assertEqual(df["date"].to_list()[0], date(2026, 9, 16))


class TestCreditParser(unittest.TestCase):
    def test_purchases_positive_refunds_negative(self):
        with tempfile.TemporaryDirectory() as directory:
            df = WealthsimpleCreditStatement(
                csv_file(
                    directory,
                    "Gol's Lanzhou Noodle,Purchase,− $18.50 CAD,2026-09-24\n"
                    "Shop,Refund,$5.00 CAD,2026-09-20\n"
                    "Credit card payment,From: Chequing,$100.00 CAD,2026-09-21\n",
                )
            ).get_df()

        self.assertEqual(
            df.select("merchant", "cost").rows(),
            [("Purchase: Gol's Lanzhou Noodle", 18.5), ("Refund: Shop", -5.0)],
        )


class TestDownloader(unittest.TestCase):
    def test_since_is_required(self):
        with self.assertRaises(SystemExit):
            downloader.parse_args(
                [
                    "--output-dir",
                    "/tmp/x",
                    "--database",
                    "finance",
                    "--account",
                    "debit",
                ]
            )

    @patch.object(downloader, "run_scrape")
    def test_missing_totp_secret_fails_before_browser(self, run_scrape):
        env = {
            "WS_EMAIL": "e",
            "WS_PASSWORD": "p",
            "WS_TOTP_SECRET": "",
            "WS_DEBIT_LINK": "x",
        }
        with patch.dict(os.environ, env), patch.object(downloader, "Config") as config:
            config.return_value.ws_debt_link = "x"
            with self.assertRaisesRegex(RuntimeError, "WS_TOTP_SECRET"):
                downloader.run(Path("/tmp/none"), "finance", "debit", date(2026, 9, 1))
        run_scrape.assert_not_called()

    @patch.object(downloader, "run_scrape")
    def test_existing_output_fails_before_browser(self, run_scrape):
        with tempfile.TemporaryDirectory() as directory:
            target = downloader.destination(Path(directory), "credit", date(2026, 9, 1))
            target.write_bytes(b"keep")
            env = {"WS_EMAIL": "e", "WS_PASSWORD": "p", "WS_TOTP_SECRET": "S"}
            with (
                patch.dict(os.environ, env),
                patch.object(downloader, "Config") as config,
            ):
                config.return_value.ws_credit_link = "x"
                with self.assertRaises(FileExistsError):
                    downloader.run(
                        Path(directory), "finance", "credit", date(2026, 9, 1)
                    )
            self.assertEqual(target.read_bytes(), b"keep")
        run_scrape.assert_not_called()

    def test_writes_validates_and_prints_loader_command(self):
        rows = [
            {
                "description": "Shop",
                "type": "Purchase",
                "amount": "− $1.00 CAD",
                "date": "2026-09-28",
            }
        ]

        async def fake_scrape(*args):
            return rows

        with tempfile.TemporaryDirectory() as directory:
            env = {
                "WS_EMAIL": "email-value",
                "WS_PASSWORD": "password-value",
                "WS_TOTP_SECRET": "totp-secret-value",
            }
            with (
                patch.dict(os.environ, env),
                patch.object(downloader, "Config") as config,
                patch.object(downloader, "run_scrape", side_effect=fake_scrape),
                self.assertLogs(downloader.logger) as logs,
            ):
                config.return_value.ws_credit_link = "x"
                saved = downloader.run(
                    Path(directory), "finance", "credit", date(2026, 9, 1)
                )
            self.assertTrue(saved.exists())
        command = downloader.loader_command(saved, "credit", "finance")
        self.assertIn(command, "\n".join(logs.output))
        self.assertIn("--type ws_credit", command)
        for secret in env.values():
            self.assertNotIn(secret, "\n".join(logs.output))


if __name__ == "__main__":
    unittest.main()

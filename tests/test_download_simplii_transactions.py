import asyncio
import os
import subprocess
import sys
import tempfile
import unittest
from datetime import date
from pathlib import Path
from unittest.mock import patch

from polars.exceptions import ColumnNotFoundError

from scripts import download_simplii_transactions as downloader

FIXTURE = Path(__file__).parent / "fixtures" / "simplii_visa.csv"


def staged(directory, body: bytes | None = None) -> Path:
    """Stage a download where the browser backend leaves it."""
    staging = Path(directory) / "browseros-download"
    staging.mkdir()
    downloaded = staging / "account activity.csv"
    downloaded.write_bytes(FIXTURE.read_bytes() if body is None else body)
    return downloaded


class TestDownloader(unittest.TestCase):
    def test_module_entrypoint_resolves_repository_imports(self):
        result = subprocess.run(
            [sys.executable, "-m", "scripts.download_simplii_transactions", "--help"],
            capture_output=True,
            text=True,
            check=False,
        )

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("--account {visa,debit}", result.stdout)
        self.assertIn("--database {finance,parents_finance}", result.stdout)

    def test_since_is_required(self):
        with self.assertRaises(SystemExit):
            downloader.parse_args(
                [
                    "--output-dir",
                    "/tmp/x",
                    "--database",
                    "finance",
                    "--account",
                    "visa",
                ]
            )

    @patch.object(downloader, "run_scrape")
    def test_missing_credentials_fail_before_browser(self, run_scrape):
        env = {"SIMPLII_USER": "", "SIMPLII_PASSWORD": ""}
        with patch.dict(os.environ, env), tempfile.TemporaryDirectory() as directory:
            with self.assertRaisesRegex(RuntimeError, "SIMPLII_USER"):
                downloader.run(Path(directory), "finance", "visa", date(2026, 9, 1))
        run_scrape.assert_not_called()

    @patch.object(downloader, "run_scrape")
    def test_existing_output_fails_before_browser(self, run_scrape):
        env = {"SIMPLII_USER": "u", "SIMPLII_PASSWORD": "p"}
        with patch.dict(os.environ, env), tempfile.TemporaryDirectory() as directory:
            target = downloader.destination(Path(directory), "visa", date(2026, 9, 1))
            target.write_bytes(b"keep")
            with self.assertRaises(FileExistsError):
                downloader.run(Path(directory), "finance", "visa", date(2026, 9, 1))
            self.assertEqual(target.read_bytes(), b"keep")
        run_scrape.assert_not_called()

    def test_moves_validates_and_prints_the_unified_loader_command(self):
        env = {"SIMPLII_USER": "user-value", "SIMPLII_PASSWORD": "password-value"}
        with patch.dict(os.environ, env), tempfile.TemporaryDirectory() as directory:
            staged_path = staged(directory)
            # A spaced output directory proves the printed handoff stays
            # copy-pasteable.
            output_dir = Path(directory) / "my output"

            async def fake_scrape(*args):
                return staged_path

            with (
                patch.object(downloader, "run_scrape", side_effect=fake_scrape),
                self.assertLogs(downloader.logger) as logs,
            ):
                saved = downloader.run(output_dir, "finance", "visa", date(2026, 9, 1))
            self.assertTrue(saved.exists())
            self.assertFalse(staged_path.exists())
        self.assertEqual(
            saved.name, f"simplii-visa-2026-09-01-{date.today().isoformat()}.csv"
        )
        output = "\n".join(logs.output)
        self.assertIn(
            "uv run --frozen python load-transactions.py --type simplii_visa "
            f"--filepath '{saved}' --database finance",
            output,
        )
        for secret in env.values():
            self.assertNotIn(secret, output)

    def test_debit_account_prints_its_own_card_type(self):
        env = {"SIMPLII_USER": "u", "SIMPLII_PASSWORD": "p"}
        with patch.dict(os.environ, env), tempfile.TemporaryDirectory() as directory:
            staged_path = staged(directory)

            async def fake_scrape(*args):
                return staged_path

            with (
                patch.object(downloader, "run_scrape", side_effect=fake_scrape),
                self.assertLogs(downloader.logger) as logs,
            ):
                saved = downloader.run(
                    Path(directory), "parents_finance", "debit", date(2026, 9, 1)
                )
        self.assertIn(
            f"uv run --frozen python load-transactions.py --type simplii_debit "
            f"--filepath {saved} --database parents_finance",
            "\n".join(logs.output),
        )

    def test_passes_account_credentials_and_last_four_to_the_bridge(self):
        env = {
            "SIMPLII_USER": "user-value",
            "SIMPLII_PASSWORD": "password-value",
            "SIMPLII_ACCOUNT": "4321",
        }
        with patch.dict(os.environ, env), tempfile.TemporaryDirectory() as directory:
            staged_path = staged(directory)

            async def fake_scrape(*args):
                return staged_path

            with patch.object(
                downloader, "run_scrape", side_effect=fake_scrape
            ) as scrape:
                downloader.run(
                    Path(directory),
                    "finance",
                    "visa",
                    date(2026, 9, 1),
                    date(2026, 9, 30),
                )
        self.assertEqual(
            scrape.call_args.args,
            (
                "visa",
                date(2026, 9, 1),
                date(2026, 9, 30),
                "user-value",
                "password-value",
                "4321",
            ),
        )

    def test_until_defaults_to_today(self):
        env = {"SIMPLII_USER": "u", "SIMPLII_PASSWORD": "p"}
        with patch.dict(os.environ, env), tempfile.TemporaryDirectory() as directory:
            staged_path = staged(directory)

            async def fake_scrape(*args):
                return staged_path

            with patch.object(
                downloader, "run_scrape", side_effect=fake_scrape
            ) as scrape:
                downloader.run(Path(directory), "finance", "visa", date(2026, 9, 1))
        self.assertEqual(scrape.call_args.args[2], date.today())

    def test_until_before_since_fails_before_browser(self):
        with patch.object(downloader, "run_scrape") as scrape:
            with self.assertRaisesRegex(ValueError, "--until"):
                downloader.run(
                    Path("/tmp"),
                    "finance",
                    "visa",
                    date(2026, 9, 30),
                    date(2026, 9, 1),
                )
        scrape.assert_not_called()

    def test_parse_args_accepts_an_optional_until(self):
        args = downloader.parse_args(
            [
                "--output-dir",
                "/tmp",
                "--database",
                "finance",
                "--account",
                "debit",
                "--since",
                "2026-09-01",
                "--until",
                "2026-09-15",
            ]
        )
        self.assertEqual(args.until, date(2026, 9, 15))
        self.assertIsNone(
            downloader.parse_args(
                [
                    "--output-dir",
                    "/tmp",
                    "--database",
                    "finance",
                    "--account",
                    "debit",
                    "--since",
                    "2026-09-01",
                ]
            ).until
        )

    def test_run_scrape_matches_the_bridge_entrypoint(self):
        # The bridge itself is exercised by its own fake-session suite; this
        # pins the boundary the downloader calls it through.
        calls = []

        async def fake_run_download(*args):
            calls.append(args)
            return FIXTURE

        with patch("scripts.simplii_browseros_bridge.run_download", fake_run_download):
            asyncio.run(
                downloader.run_scrape(
                    "visa", date(2026, 9, 1), date(2026, 10, 10), "u", "p", "4321"
                )
            )

        self.assertEqual(
            calls,
            [("visa", date(2026, 9, 1), date(2026, 10, 10), "u", "p", "4321")],
        )

    def test_an_unreadable_export_leaves_no_file_behind(self):
        # Both Simplii parsers normalize the export's columns, so a changed
        # export shows up as a column the parser needs but cannot find; the run
        # must fail instead of handing the loader a malformed statement
        # (V5, V6).
        body = "Date, Description, Amount\n01/15/2026, COFFEE SHOP, 12.34\n"
        env = {"SIMPLII_USER": "u", "SIMPLII_PASSWORD": "p"}
        with patch.dict(os.environ, env), tempfile.TemporaryDirectory() as directory:
            staged_path = staged(directory, body.encode())

            async def fake_scrape(*args):
                return staged_path

            with patch.object(downloader, "run_scrape", side_effect=fake_scrape):
                with self.assertRaises(ColumnNotFoundError):
                    downloader.run(Path(directory), "finance", "visa", date(2026, 9, 1))
            self.assertEqual(list(Path(directory).glob("simplii-*.csv")), [])


if __name__ == "__main__":
    unittest.main()

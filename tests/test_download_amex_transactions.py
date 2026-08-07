import subprocess
import sys
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest.mock import MagicMock, patch

from scripts import download_amex_transactions as downloader
from scripts.download_amex_transactions import (
    loader_command,
    parse_args,
    run_osascript,
    sanitize_filename,
)


class TestDownloadAmexTransactions(unittest.TestCase):
    def test_v12_module_entrypoint_resolves_repository_imports(self) -> None:
        result = subprocess.run(
            [sys.executable, "-m", "scripts.download_amex_transactions", "--help"],
            capture_output=True,
            text=True,
            check=False,
        )

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("--database {finance,parents_finance}", result.stdout)

    def test_required_cli_arguments(self) -> None:
        args = parse_args(
            ["--output-dir", "/tmp/amex", "--database", "parents_finance"]
        )

        self.assertEqual(args.output_dir, Path("/tmp/amex"))
        self.assertEqual(args.database, "parents_finance")

    def test_sanitize_filename_removes_path_and_unsafe_characters(self) -> None:
        self.assertEqual(sanitize_filename("../../activity (1).csv"), "activity_1_.csv")

    @patch("scripts.download_amex_transactions.subprocess.run")
    def test_osascript_error_is_actionable(self, run: MagicMock) -> None:
        run.return_value = MagicMock(
            returncode=1, stderr="Apple Events disabled", stdout=""
        )

        with self.assertRaisesRegex(RuntimeError, "Apple Events disabled"):
            run_osascript("return true")

    @patch("scripts.download_amex_transactions.subprocess.run")
    def test_v15_osascript_timeout_is_actionable(self, run: MagicMock) -> None:
        run.side_effect = subprocess.TimeoutExpired("osascript", 5)

        with self.assertRaisesRegex(RuntimeError, "AppleScript timed out"):
            run_osascript("return true")

    def test_loader_handoff_quotes_path(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "activity export.csv"

            command = loader_command(path, "finance")

            self.assertEqual(
                command,
                "uv run python load-transactions.py --type amex --filepath "
                f"'{path}' --database finance",
            )

    @patch("scripts.download_amex_transactions.run_browse")
    def test_browserbase_download_extracts_single_csv(
        self, run_browse: MagicMock
    ) -> None:
        fixture = Path(__file__).parent / "fixtures" / "activity.csv"

        def write_archive(args: list[str], **_: object) -> str:
            archive = Path(args[args.index("--output") + 1])
            with zipfile.ZipFile(archive, "w") as output:
                output.writestr("activity export.csv", fixture.read_bytes())
            return ""

        run_browse.side_effect = write_archive
        with tempfile.TemporaryDirectory() as directory:
            saved = downloader.browserbase_download(
                "session-id", Path(directory) / "output"
            )

            self.assertEqual(saved.name, "activity_export.csv")
            self.assertTrue(saved.exists())
        run_browse.assert_called_once()


if __name__ == "__main__":
    unittest.main()

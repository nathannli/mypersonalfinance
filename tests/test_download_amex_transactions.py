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

    def test_statement_months_are_parsed(self) -> None:
        args = parse_args(
            [
                "--output-dir",
                "/tmp/amex",
                "--database",
                "finance",
                "--months",
                "2026-05",
                "2026-06",
                "latest",
            ]
        )

        self.assertEqual(args.months, ["2026-05", "2026-06", "latest"])

    def test_browser_backend_defaults_to_browserbase(self) -> None:
        with patch.dict("os.environ", {}, clear=True):
            self.assertEqual(downloader.amex_browser_backend(), "browserbase")

    def test_browser_backend_accepts_browseros(self) -> None:
        with patch.dict("os.environ", {"AMEX_BROWSER_BACKEND": "browseros"}):
            self.assertEqual(downloader.amex_browser_backend(), "browseros")

    def test_browser_backend_rejects_unknown_value(self) -> None:
        with patch.dict("os.environ", {"AMEX_BROWSER_BACKEND": "chrome"}):
            with self.assertRaisesRegex(RuntimeError, "AMEX_BROWSER_BACKEND"):
                downloader.amex_browser_backend()

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

    @patch("scripts.download_amex_transactions.time.sleep")
    @patch("scripts.download_amex_transactions.run_browse")
    def test_browserbase_download_polls_until_csv(
        self, run_browse: MagicMock, _sleep: MagicMock
    ) -> None:
        fixture = Path(__file__).parent / "fixtures" / "activity.csv"

        def write_archive(args: list[str], **_: object) -> str:
            archive = Path(args[args.index("--output") + 1])
            with zipfile.ZipFile(archive, "w") as output:
                if run_browse.call_count > 1:
                    output.writestr("activity.csv", fixture.read_bytes())
            return ""

        run_browse.side_effect = write_archive
        with tempfile.TemporaryDirectory() as directory:
            saved = downloader.browserbase_download(
                "session-id", Path(directory) / "output"
            )

        self.assertEqual(saved.name, "activity.csv")
        self.assertEqual(run_browse.call_count, 2)

    def test_browseros_route_returns_bridge_downloads(self) -> None:
        async def bridge_download(*_args: object) -> dict[str, object]:
            return {"status": "downloaded", "paths": ["/tmp/amex-2026-07.csv"]}

        with (
            patch(
                "scripts.amex_browseros_bridge.run_download",
                side_effect=bridge_download,
            ),
            patch.dict("os.environ", {"AMEX_BROWSER_BACKEND": "browseros"}),
        ):
            saved = downloader.run(Path("/tmp/amex"), "finance", ["2026-07"])

        self.assertEqual(saved, [Path("/tmp/amex-2026-07.csv")])


if __name__ == "__main__":
    unittest.main()

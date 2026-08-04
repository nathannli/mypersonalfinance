import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from scripts import download_amex_transactions as downloader

FIXTURES = Path(__file__).parent / "fixtures"


class TestAmexBrowserWorkflow(unittest.TestCase):
    def test_local_html_fixture_documents_accessible_controls(self) -> None:
        html = (FIXTURES / "amex_statement_page.html").read_text()

        for label in ("Card", "Statement", "Export Statement Data", "CSV", "Download"):
            self.assertIn(label, html)

    def test_wait_for_authentication_accepts_authenticated_redirect(self) -> None:
        page = MagicMock(url="https://global.americanexpress.com/dashboard")

        downloader.wait_for_authentication(page)

        page.wait_for_url.assert_not_called()

    def test_wait_for_authentication_reports_timeout(self) -> None:
        page = MagicMock(url=downloader.LOGIN_URL)
        page.wait_for_url.side_effect = TimeoutError("timed out")

        with self.assertRaisesRegex(RuntimeError, "authenticated Amex"):
            downloader.wait_for_authentication(page)

    @patch.object(downloader, "click_named")
    @patch.object(downloader, "ensure_single_card")
    def test_navigation_uses_statement_export_path(
        self, ensure_single_card: MagicMock, click_named: MagicMock
    ) -> None:
        page = MagicMock()

        downloader.navigate_to_export(page)

        ensure_single_card.assert_called_once_with(page)
        self.assertEqual(
            click_named.call_args_list,
            [
                unittest.mock.call(page, "Statement"),
                unittest.mock.call(page, "Export Statement Data"),
            ],
        )

    def test_multiple_cards_exit_with_actionable_error(self) -> None:
        page = MagicMock()
        selector = MagicMock()
        selector.count.return_value = 1
        page.get_by_role.side_effect = lambda role, **_: (
            selector if role == "combobox" else MagicMock(count=lambda: 2)
        )

        with self.assertRaisesRegex(RuntimeError, "Expected one Amex card; found 2"):
            downloader.ensure_single_card(page)

    def test_csv_radio_is_selected(self) -> None:
        page = MagicMock()
        radio = MagicMock()
        radio.count.return_value = 1
        page.get_by_role.return_value = radio

        downloader.select_csv(page)

        radio.first.check.assert_called_once_with(timeout=downloader.ACTION_TIMEOUT_MS)

    def test_successful_download_has_no_partial_file(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            page = MagicMock()
            download = MagicMock(suggested_filename="activity.csv")
            page.expect_download.return_value.__enter__.return_value.value = download
            download.save_as.side_effect = lambda path: Path(path).write_bytes(b"csv")

            saved = downloader.capture_download(page, Path(directory))

            self.assertEqual(saved.read_bytes(), b"csv")
            self.assertEqual(
                [path.name for path in Path(directory).iterdir()], ["activity.csv"]
            )

    def test_download_timeout_leaves_no_file(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            page = MagicMock()
            page.expect_download.side_effect = TimeoutError("timed out")

            with self.assertRaisesRegex(RuntimeError, "timed out"):
                downloader.capture_download(page, Path(directory))

            self.assertEqual(list(Path(directory).iterdir()), [])

    def test_v11_synthetic_download_parses_exact_contract(self) -> None:
        path = FIXTURES / "activity.csv"

        downloader.validate_download(path)

        from sources.csv.amex import AmexStatement

        df = AmexStatement(str(path)).get_df()
        self.assertEqual(df.columns, ["date", "merchant", "cost", "cc_category"])
        self.assertEqual(df.height, 1)

    def test_run_uses_headed_persistent_context_and_always_closes(self) -> None:
        manager = MagicMock()
        playwright = manager.__enter__.return_value
        context = playwright.chromium.launch_persistent_context.return_value
        context.pages = [MagicMock()]
        sync_api = types.ModuleType("playwright.sync_api")
        sync_api.sync_playwright = MagicMock(return_value=manager)
        playwright_package = types.ModuleType("playwright")
        playwright_package.sync_api = sync_api

        with (
            tempfile.TemporaryDirectory() as directory,
            patch.dict(
                sys.modules,
                {"playwright": playwright_package, "playwright.sync_api": sync_api},
            ),
            patch.object(downloader, "wait_for_authentication"),
            patch.object(downloader, "navigate_to_export"),
            patch.object(downloader, "select_csv"),
            patch.object(
                downloader,
                "capture_download",
                return_value=FIXTURES / "activity.csv",
            ),
            patch.object(
                downloader, "validate_download", side_effect=ValueError("bad file")
            ),
            self.assertRaisesRegex(ValueError, "bad file"),
        ):
            downloader.run(Path(directory), "finance")

        playwright.chromium.launch_persistent_context.assert_called_once_with(
            downloader.AMEX_BROWSER_PROFILE_DIR,
            headless=False,
            accept_downloads=True,
        )
        context.close.assert_called_once_with()


if __name__ == "__main__":
    unittest.main()

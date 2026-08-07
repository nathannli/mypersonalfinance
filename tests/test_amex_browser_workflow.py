import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, call, patch

from scripts import download_amex_transactions as downloader

FIXTURES = Path(__file__).parent / "fixtures"


class TestAmexBrowserWorkflow(unittest.TestCase):
    def test_local_html_fixture_documents_accessible_controls(self) -> None:
        html = (FIXTURES / "amex_statement_page.html").read_text()

        self.assertIn('<select id="card">', html)
        self.assertIn('<a href="/statement">Statement</a>', html)
        self.assertIn("<button>Export Statement Data</button>", html)
        self.assertIn('<a href="/activity">Go to Statement Activity</a>', html)
        self.assertIn('<div role="dialog" aria-modal="true">', html)
        self.assertIn('<input type="radio" name="format" value="csv">', html)
        self.assertIn('<a href="/download">Download</a>', html)

    @patch.object(
        downloader, "find_amex_url", return_value=downloader.AUTHENTICATED_URL
    )
    def test_wait_for_authentication_reuses_normal_chrome(
        self, find_url: MagicMock
    ) -> None:
        with patch.object(downloader.subprocess, "run") as run:
            downloader.wait_for_authentication()

        find_url.assert_called_once_with()
        run.assert_not_called()

    @patch.object(
        downloader, "run_osascript", return_value=downloader.AUTHENTICATED_URL
    )
    def test_v16_authenticated_tab_has_priority(self, run_osascript: MagicMock) -> None:
        self.assertEqual(downloader.find_amex_url(), downloader.AUTHENTICATED_URL)

        script = run_osascript.call_args.args[0]
        self.assertLess(
            script.index('starts with "https://global.americanexpress.com/"'),
            script.index('contains "americanexpress.com"'),
        )

    @patch.object(downloader, "run_osascript", return_value="clicked")
    def test_click_visible_uses_exact_dom_text(self, run_osascript: MagicMock) -> None:
        downloader.click_visible("Statement")

        script = run_osascript.call_args.args[0]
        self.assertIn("=== expected", script)
        self.assertIn('const expected = \\"Statement\\"', script)

    @patch.object(downloader, "execute_chrome_js", return_value="2")
    def test_multiple_cards_exit_with_actionable_error(
        self, execute_js: MagicMock
    ) -> None:
        with self.assertRaisesRegex(RuntimeError, "Expected one Amex card; found 2"):
            downloader.ensure_single_card()

        execute_js.assert_called_once()

    @patch.object(downloader, "click_visible")
    @patch.object(downloader, "navigate_visible_link")
    @patch.object(downloader, "ensure_single_card")
    @patch.object(downloader, "execute_chrome_js")
    @patch.object(downloader, "wait_until")
    def test_navigation_uses_current_amex_path(
        self,
        wait_until: MagicMock,
        execute_js: MagicMock,
        ensure_single_card: MagicMock,
        navigate_visible_link: MagicMock,
        click_visible: MagicMock,
    ) -> None:
        execute_js.return_value = "/dashboard"

        downloader.navigate_to_export()

        ensure_single_card.assert_called_once_with()
        self.assertEqual(
            click_visible.call_args_list,
            [call("Statement")],
        )
        self.assertEqual(wait_until.call_count, 4)

    @patch.object(downloader, "ensure_single_card")
    @patch.object(downloader, "execute_chrome_js", return_value="/activity")
    def test_navigation_checks_card_when_already_on_activity(
        self, execute_js: MagicMock, ensure_single_card: MagicMock
    ) -> None:
        downloader.navigate_to_export()

        execute_js.assert_called_once_with("location.pathname")
        ensure_single_card.assert_called_once_with()

    @patch.object(downloader, "click_visible")
    @patch.object(downloader, "wait_until")
    @patch.object(downloader, "execute_chrome_js")
    def test_csv_selection_returns_native_click_point(
        self, execute_js: MagicMock, wait_until: MagicMock, click_visible: MagicMock
    ) -> None:
        execute_js.side_effect = ["true", json.dumps({"x": 967, "y": 887})]

        point = downloader.select_csv_and_get_download_point()

        self.assertEqual(point, (967, 887))
        click_visible.assert_not_called()
        wait_until.assert_called_once()
        script = execute_js.call_args.args[0]
        self.assertIn("csv.closest('[role=dialog]')", script)
        self.assertIn("dialog.querySelectorAll('a')", script)
        self.assertIn("element.offsetParent !== null", script)
        self.assertIn("getBoundingClientRect", script)

    @patch.object(downloader.subprocess, "run")
    def test_v14_native_click_uses_core_graphics(self, run: MagicMock) -> None:
        run.return_value = MagicMock(returncode=0)

        downloader.native_click(967, 887)

        command = run.call_args.args[0]
        self.assertEqual(command[:2], ["swift", "-e"])
        self.assertIn("CGPoint(x: 967, y: 887)", command[2])
        self.assertIn("leftMouseDown", command[2])
        self.assertIn("leftMouseUp", command[2])

    def test_successful_download_moves_without_partial_file(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            download = root / "activity (1).csv"
            download.write_bytes((FIXTURES / "activity.csv").read_bytes())
            output = root / "output"

            saved = downloader.move_download(download, output)

            self.assertEqual(saved.name, "activity_1_.csv")
            self.assertTrue(saved.exists())
            self.assertFalse(download.exists())

    @patch.object(downloader.shutil, "copyfileobj", side_effect=OSError("copy failed"))
    def test_failed_download_copy_removes_reserved_destination(
        self, copyfileobj: MagicMock
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            download = root / "activity.csv"
            download.write_text("source")
            output = root / "output"

            with self.assertRaisesRegex(OSError, "copy failed"):
                downloader.move_download(download, output)

            copyfileobj.assert_called_once()
            self.assertTrue(download.exists())
            self.assertFalse((output / "activity.csv").exists())

    def test_collision_refuses_overwrite(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            download = root / "activity.csv"
            download.write_text("new")
            output = root / "output"
            output.mkdir()
            (output / "activity.csv").write_text("existing")

            with self.assertRaisesRegex(FileExistsError, "Refusing to overwrite"):
                downloader.move_download(download, output)

            self.assertEqual((output / "activity.csv").read_text(), "existing")

    @patch.object(downloader.time, "sleep")
    @patch.object(downloader.time, "monotonic", side_effect=[0, 31])
    def test_download_timeout_removes_new_partial(
        self, monotonic: MagicMock, sleep: MagicMock
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            partial = root / "activity.csv.crdownload"
            partial.touch()

            with self.assertRaisesRegex(RuntimeError, "Timed out waiting"):
                downloader.wait_for_download(root, set())

            self.assertFalse(partial.exists())

    def test_v11_synthetic_download_parses_exact_contract(self) -> None:
        path = FIXTURES / "activity.csv"

        downloader.validate_download(path)

        from sources.csv.amex import AmexStatement

        df = AmexStatement(str(path)).get_df()
        self.assertEqual(df.columns, ["date", "merchant", "cost", "cc_category"])
        self.assertEqual(df.height, 1)

    def test_v11_normalizes_amex_csv_text_before_parsing(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "activity.csv"
            path.write_text(
                "Date,Description,Amount\n"
                "\xa003 Aug 2026\xa0,\xa0MERCHANT ONE\xa0,\xa0$12.34\xa0\n"
                "04 Aug 2026,PAYMENT RECEIVED\xa0- THANK YOU,-12.34\n"
            )

            from sources.csv.amex import AmexStatement

            df = AmexStatement(str(path)).get_df()

        self.assertEqual(df.height, 1)
        self.assertEqual(df.item(0, "merchant"), "MERCHANT ONE")

    @patch.object(downloader, "validate_download")
    @patch.object(downloader, "move_download")
    @patch.object(downloader, "wait_for_download")
    @patch.object(downloader, "native_click")
    @patch.object(downloader, "select_csv_and_get_download_point", return_value=(1, 2))
    @patch.object(downloader, "chrome_download_dir")
    @patch.object(downloader, "navigate_to_export")
    @patch.object(downloader, "wait_for_authentication")
    def test_run_orchestrates_normal_chrome_without_loading_database(
        self,
        wait_for_authentication: MagicMock,
        navigate_to_export: MagicMock,
        chrome_download_dir: MagicMock,
        select_point: MagicMock,
        native_click: MagicMock,
        wait_for_download: MagicMock,
        move_download: MagicMock,
        validate_download: MagicMock,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            chrome_download_dir.return_value = root
            downloaded = root / "activity.csv"
            wait_for_download.return_value = downloaded
            saved = root / "output" / "activity.csv"
            move_download.return_value = saved

            result = downloader.run(root / "output", "finance")

        self.assertEqual(result, saved)
        wait_for_authentication.assert_called_once_with()
        navigate_to_export.assert_called_once_with()
        native_click.assert_called_once_with(1, 2)
        validate_download.assert_called_once_with(saved)


if __name__ == "__main__":
    unittest.main()

import json
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import MagicMock, call, patch

from scripts import download_amex_transactions as downloader

FIXTURES = Path(__file__).parent / "fixtures"


class TestAmexBrowserWorkflow(unittest.TestCase):
    @patch.object(
        downloader,
        "run_browse",
        return_value='{"url": "https://global.americanexpress.com/dashboard"}',
    )
    def test_browserbase_extracts_url_from_json(self, run_browse: MagicMock) -> None:
        self.assertEqual(
            downloader.browse_url(), "https://global.americanexpress.com/dashboard"
        )
        run_browse.assert_called_once_with(["get", "url"])

    @patch.object(downloader.subprocess, "run")
    def test_browserbase_commands_use_named_remote_session(
        self, run: MagicMock
    ) -> None:
        run.return_value = MagicMock(
            returncode=0, stderr="", stdout='{"url": "https://example.com"}'
        )

        with patch.dict("os.environ", {"BROWSERBASE_API_KEY": "test-key"}):
            downloader.run_browse(["get", "url"])

        self.assertEqual(
            run.call_args.args[0],
            [
                "browse",
                "get",
                "url",
                "--remote",
                "--session",
                downloader.BROWSE_SESSION,
            ],
        )

    @patch.object(downloader, "run_browse", return_value='{"result": "1"}')
    def test_browserbase_extracts_evaluation_result_from_json(
        self, run_browse: MagicMock
    ) -> None:
        self.assertEqual(downloader.browse_eval("String(1)"), "1")
        run_browse.assert_called_once_with(["eval", "String(1)"])

    @patch.object(downloader.time, "sleep")
    @patch.object(downloader, "dismiss_browserbase_popups")
    @patch.object(
        downloader,
        "browse_url",
        side_effect=[
            "https://global.americanexpress.com/activity/statements",
            "https://global.americanexpress.com/activity",
        ],
    )
    def test_browserbase_waits_for_exact_activity_path(
        self,
        browse_url: MagicMock,
        dismiss_popups: MagicMock,
        sleep: MagicMock,
    ) -> None:
        url = downloader.wait_for_browserbase_url(
            "/activity", "Amex statement activity page in Browserbase"
        )

        self.assertEqual(url, "https://global.americanexpress.com/activity")
        self.assertEqual(browse_url.call_count, 2)
        self.assertEqual(dismiss_popups.call_count, 2)

    @patch.object(downloader, "browse_eval", return_value="Download")
    @patch.object(
        downloader,
        "browse_url",
        return_value="https://global.americanexpress.com/activity?days=30",
    )
    @patch.object(downloader, "dismiss_browserbase_popups")
    def test_browserbase_waits_for_statement_activity_page(
        self,
        dismiss_popups: MagicMock,
        browse_url: MagicMock,
        browse_eval: MagicMock,
    ) -> None:
        downloader.wait_for_statement_activity_browserbase()

        dismiss_popups.assert_called_once_with()
        browse_url.assert_called_once_with()
        browse_eval.assert_called_once_with("document.body.innerText")

    @patch.object(downloader.subprocess, "run")
    def test_browserbase_recovers_no_active_page_once(self, run: MagicMock) -> None:
        run.side_effect = [
            MagicMock(
                returncode=1,
                stderr=downloader.NO_ACTIVE_PAGE_ERROR,
                stdout="",
            ),
            MagicMock(returncode=0, stderr="", stdout=""),
            MagicMock(
                returncode=0,
                stderr="",
                stdout="https://global.americanexpress.com/dashboard",
            ),
        ]

        with patch.dict("os.environ", {"BROWSERBASE_API_KEY": "test-key"}):
            url = downloader.run_browse(["get", "url"])

        self.assertEqual(url, "https://global.americanexpress.com/dashboard")
        self.assertEqual(
            run.call_args_list,
            [
                call(
                    [
                        "browse",
                        "get",
                        "url",
                        "--remote",
                        "--session",
                        downloader.BROWSE_SESSION,
                    ],
                    text=True,
                    capture_output=True,
                    check=False,
                    timeout=downloader.ACTION_TIMEOUT_SECONDS,
                ),
                call(
                    [
                        "browse",
                        "open",
                        downloader.AUTHENTICATED_URL,
                        "--remote",
                        "--session",
                        downloader.BROWSE_SESSION,
                    ],
                    text=True,
                    capture_output=True,
                    check=False,
                    timeout=downloader.REMOTE_OPEN_TIMEOUT_SECONDS,
                ),
                call(
                    [
                        "browse",
                        "get",
                        "url",
                        "--remote",
                        "--session",
                        downloader.BROWSE_SESSION,
                    ],
                    text=True,
                    capture_output=True,
                    check=False,
                    timeout=downloader.ACTION_TIMEOUT_SECONDS,
                ),
            ],
        )

    @patch.object(downloader, "run_browse")
    def test_browserbase_retries_with_fresh_session_after_daemon_timeout(
        self, run_browse: MagicMock
    ) -> None:
        run_browse.side_effect = [
            RuntimeError(
                f"Browserbase command failed: {downloader.DRIVER_DAEMON_TIMEOUT}"
            ),
            "",
            json.dumps(
                {
                    "browserbaseSessionId": "fresh-session",
                    "browserbaseSessionUrl": "https://browserbase.test/fresh-session",
                }
            ),
        ]

        session_id, session_url = downloader.browse_open()

        self.assertEqual(session_id, "fresh-session")
        self.assertEqual(session_url, "https://browserbase.test/fresh-session")
        self.assertEqual(
            run_browse.call_args_list,
            [
                call(
                    ["open", downloader.LOGIN_URL],
                    timeout=downloader.REMOTE_OPEN_TIMEOUT_SECONDS,
                ),
                call(
                    ["stop", "--force", "--session", downloader.BROWSE_SESSION],
                    remote=False,
                ),
                call(
                    ["open", downloader.LOGIN_URL],
                    timeout=downloader.REMOTE_OPEN_TIMEOUT_SECONDS,
                ),
            ],
        )

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

    @patch.object(downloader.time, "sleep")
    @patch.object(
        downloader,
        "browse_url",
        side_effect=[
            "https://www.americanexpress.com/en-ca/account/login/",
            f"{downloader.AUTHENTICATED_URL}dashboard",
        ],
    )
    @patch.object(
        downloader,
        "browse_open",
        return_value=("browserbase-session", "https://browserbase.test/session"),
    )
    @patch.object(downloader, "fill_browserbase_credentials")
    @patch.object(downloader, "browse_click_xpath")
    @patch.object(downloader, "select_sms_mfa_browserbase")
    @patch.object(downloader, "submit_sms_mfa_browserbase")
    @patch.object(downloader, "complete_trust_device_browserbase")
    def test_browserbase_auth_waits_for_authenticated_url(
        self,
        complete_trust_device: MagicMock,
        submit_sms: MagicMock,
        select_sms: MagicMock,
        browse_click: MagicMock,
        fill_credentials: MagicMock,
        browse_open: MagicMock,
        browse_url: MagicMock,
        sleep: MagicMock,
    ) -> None:
        with patch.dict("os.environ", {"BROWSERBASE_API_KEY": "test-key"}):
            session_id = downloader.wait_for_browserbase_authentication()

        self.assertEqual(session_id, "browserbase-session")
        browse_open.assert_called_once_with()
        fill_credentials.assert_called_once_with()
        browse_click.assert_called_once_with('//button[normalize-space(.)="Log In"]')
        select_sms.assert_called_once_with()
        submit_sms.assert_called_once_with(select_sms.return_value)
        complete_trust_device.assert_called_once_with()
        self.assertEqual(browse_url.call_count, 2)

    @patch.object(downloader, "browse_fill")
    def test_browserbase_fills_credentials_from_environment(
        self, browse_fill: MagicMock
    ) -> None:
        with patch.dict(
            "os.environ",
            {"AMEX_USER": "test-user", "AMEX_PASSWORD": "test-password"},
        ):
            downloader.fill_browserbase_credentials()

        self.assertEqual(
            browse_fill.call_args_list,
            [
                call(
                    "#eliloUserID",
                    "test-user",
                    "AMEX_USER",
                ),
                call(
                    '//input[@type="password"][1]',
                    "test-password",
                    "AMEX_PASSWORD",
                ),
            ],
        )

    @patch.object(
        downloader,
        "browse_eval",
        return_value="Select how you'd like to receive your code:",
    )
    @patch.object(downloader, "browse_click_xpath")
    def test_browserbase_selects_sms_mfa(
        self, browse_click: MagicMock, browse_eval: MagicMock
    ) -> None:
        requested_at = downloader.select_sms_mfa_browserbase()

        browse_eval.assert_called_once_with("document.body.innerText")
        self.assertIsNone(requested_at.tzinfo)
        self.assertLess(
            abs(
                (
                    requested_at - datetime.now(timezone.utc).replace(tzinfo=None)
                ).total_seconds()
            ),
            1,
        )
        self.assertEqual(
            browse_click.call_args_list,
            [
                call('//form//input[@type="radio"][1]'),
                call('//button[normalize-space(.)="Continue"]'),
            ],
        )

    @patch.object(downloader.time, "sleep")
    @patch.object(downloader.messages, "get_db")
    def test_browserbase_reads_recent_amex_sms_code(
        self, get_db: MagicMock, sleep: MagicMock
    ) -> None:
        message = MagicMock(is_from_me=False, text="AMEX: your code is 315238")
        get_db.return_value.chats.return_value = [MagicMock(id=42)]
        get_db.return_value.messages.return_value = [message]

        code = downloader.wait_for_amex_sms_code(datetime.now())

        self.assertEqual(code, "315238")
        sleep.assert_called_once_with(downloader.MFA_CODE_INITIAL_DELAY_SECONDS)
        get_db.return_value.messages.assert_called_once_with(
            chat_ids=[42],
            after=unittest.mock.ANY,
            limit=25,
            include_unsent=False,
            include_unknown_senders=True,
        )

    @patch.object(downloader, "wait_for_amex_sms_code", return_value="315238")
    @patch.object(downloader, "browse_fill")
    @patch.object(downloader, "browse_click_xpath")
    @patch.object(
        downloader, "browse_eval", return_value="Please enter the verification code"
    )
    def test_browserbase_submits_sms_mfa_code(
        self,
        browse_eval: MagicMock,
        browse_click: MagicMock,
        browse_fill: MagicMock,
        wait_for_code: MagicMock,
    ) -> None:
        requested_at = datetime.now()

        downloader.submit_sms_mfa_browserbase(requested_at)

        wait_for_code.assert_called_once_with(requested_at)
        browse_fill.assert_called_once_with(
            '//form//input[@type="text"][1]', "315238", "Amex MFA code"
        )
        browse_click.assert_called_once_with('//button[normalize-space(.)="Continue"]')

    @patch.object(
        downloader,
        "browse_eval",
        side_effect=["Transfer points. Get 30% more.", ""],
    )
    def test_browserbase_dismisses_post_auth_popups(
        self, browse_eval: MagicMock
    ) -> None:
        downloader.dismiss_browserbase_popups()

        self.assertEqual(browse_eval.call_count, 2)

    @patch.object(downloader, "browse_click_xpath")
    @patch.object(
        downloader,
        "browse_eval",
        return_value="Security Verification: Trust Device",
    )
    @patch.object(
        downloader,
        "browse_url",
        return_value="https://www.americanexpress.com/en-ca/account/two-step-verification/verify",
    )
    def test_browserbase_continues_trust_device_without_trusting_device(
        self,
        browse_url: MagicMock,
        browse_eval: MagicMock,
        browse_click: MagicMock,
    ) -> None:
        downloader.complete_trust_device_browserbase()

        browse_eval.assert_called_once_with("document.body.innerText")
        browse_click.assert_called_once_with('//button[normalize-space(.)="Continue"]')

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
    @patch.object(downloader, "browserbase_download")
    @patch.object(downloader, "select_csv_browserbase")
    @patch.object(downloader, "navigate_to_export_browserbase")
    @patch.object(
        downloader, "wait_for_browserbase_authentication", return_value="session-id"
    )
    @patch.object(downloader, "cleanup_browserbase_session")
    def test_run_orchestrates_browserbase_without_loading_database(
        self,
        cleanup_session: MagicMock,
        wait_for_authentication: MagicMock,
        navigate_to_export: MagicMock,
        select_csv: MagicMock,
        browserbase_download: MagicMock,
        validate_download: MagicMock,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            saved = root / "output" / "activity.csv"
            browserbase_download.return_value = saved

            with patch.dict("os.environ", {"AMEX_BROWSER_BACKEND": "browserbase"}):
                result = downloader.run(root / "output", "finance")

        self.assertEqual(result, saved)
        wait_for_authentication.assert_called_once_with()
        navigate_to_export.assert_called_once_with(None)
        select_csv.assert_called_once_with()
        browserbase_download.assert_called_once_with("session-id", root / "output")
        validate_download.assert_called_once_with(saved)
        cleanup_session.assert_called_once_with("session-id")

    @patch.object(downloader, "cleanup_browserbase_session")
    @patch.object(
        downloader,
        "wait_for_browserbase_authentication",
        side_effect=KeyboardInterrupt,
    )
    def test_run_cleans_up_browserbase_session_after_interrupt(
        self, wait_for_authentication: MagicMock, cleanup_session: MagicMock
    ) -> None:
        with (
            patch.dict("os.environ", {"AMEX_BROWSER_BACKEND": "browserbase"}),
            self.assertRaises(KeyboardInterrupt),
        ):
            downloader.run(Path("/tmp/amex"), "finance")

        wait_for_authentication.assert_called_once_with()
        cleanup_session.assert_called_once_with(None)

    @patch.object(downloader, "run")
    @patch.object(downloader.logging, "basicConfig")
    def test_main_configures_timestamped_logger(
        self, basic_config: MagicMock, run: MagicMock
    ) -> None:
        self.assertEqual(
            downloader.main(["--output-dir", "/tmp/amex", "--database", "finance"]),
            0,
        )

        basic_config.assert_called_once_with(
            level=downloader.logging.INFO,
            format="%(asctime)s-%(name)s: %(message)s",
            datefmt="%Y-%m-%d %H:%M:%S",
        )


if __name__ == "__main__":
    unittest.main()

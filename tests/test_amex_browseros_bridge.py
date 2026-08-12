import asyncio
import os
import tempfile
import unittest
from contextlib import AbstractAsyncContextManager
from pathlib import Path
from unittest.mock import MagicMock, patch

from scripts import amex_browseros_bridge as bridge


class FakeBrowserOS(AbstractAsyncContextManager):
    def __init__(self, responses):
        self.responses = iter(responses)
        self.calls = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return None

    async def call(self, tool_name, arguments):
        self.calls.append((tool_name, arguments))
        return next(self.responses)


class TestAmexBrowserOSBridge(unittest.TestCase):
    def test_uses_neo_default_mcp_endpoint(self):
        with patch.dict(os.environ, {}, clear=True):
            self.assertEqual(bridge.browseros_mcp_url(), "http://127.0.0.1:9010/mcp")

    def test_rejects_non_loopback_browseros_endpoint(self):
        with patch.dict(os.environ, {"BROWSEROS_MCP_URL": "https://example.com/mcp"}):
            with self.assertRaisesRegex(RuntimeError, "loopback"):
                bridge.browseros_mcp_url()

    @patch.object(bridge, "endpoint_listening", side_effect=[False, True])
    @patch.object(bridge.subprocess, "run")
    def test_starts_browseros_when_mcp_endpoint_is_not_listening(
        self, run: MagicMock, endpoint_listening: MagicMock
    ) -> None:
        bridge.ensure_browseros_mcp("http://127.0.0.1:9010/mcp")

        run.assert_called_once_with(
            ["open", "-a", "BrowserOS neo"],
            check=True,
            timeout=10,
        )
        self.assertEqual(endpoint_listening.call_count, 2)

    @patch.object(bridge, "endpoint_listening", return_value=True)
    @patch.object(bridge.subprocess, "run")
    def test_does_not_restart_listening_browseros(
        self, run: MagicMock, _endpoint_listening: MagicMock
    ) -> None:
        bridge.ensure_browseros_mcp("http://127.0.0.1:9010/mcp")

        run.assert_not_called()

    def test_rejects_non_amex_login_page(self):
        for host in (
            "example.com",
            "evilamericanexpress.com",
            "americanexpress.com.attacker.test",
        ):
            snapshot = f"[UNTRUSTED_PAGE_CONTENT origin=https://{host}/login/]"
            with self.assertRaisesRegex(RuntimeError, "outside americanexpress.com"):
                bridge.require_amex_url(snapshot)

    def test_amex_origin_uses_snapshot_header_only(self):
        snapshot = (
            "[UNTRUSTED_PAGE_CONTENT origin=https://global.americanexpress.com/dashboard]\n"
            "origin=https://evilamericanexpress.com/login"
        )

        self.assertEqual(
            bridge.require_amex_url(snapshot),
            "https://global.americanexpress.com/dashboard",
        )

    def test_download_path_requires_browseros_download_directory(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            downloaded = root / "download-a" / "activity.csv"
            downloaded.parent.mkdir()
            downloaded.touch()
            with patch.dict(os.environ, {"BROWSEROS_DOWNLOAD_DIR": str(root)}):
                self.assertEqual(
                    bridge.download_path({"path": str(downloaded)}),
                    downloaded.resolve(),
                )
                with self.assertRaisesRegex(
                    RuntimeError, "outside its download directory"
                ):
                    bridge.download_path({"path": "/tmp/activity.csv"})

    def test_login_fill_uses_fresh_amex_refs(self):
        login_snapshot = """[UNTRUSTED_PAGE_CONTENT origin=https://www.americanexpress.com/en-ca/account/login/]
- textbox \"User ID\" [ref=e10]
- textbox \"Password\" [ref=e11]
- button \"Log In\" [ref=e12]"""
        browser = FakeBrowserOS([None, None])

        asyncio.run(bridge.fill_login(browser, 3, login_snapshot, "user", "password"))

        self.assertEqual(
            browser.calls,
            [
                (
                    "act",
                    {
                        "page": 3,
                        "kind": "fill",
                        "fields": [
                            {"ref": "e10", "value": "user"},
                            {"ref": "e11", "value": "password"},
                        ],
                    },
                ),
                ("act", {"page": 3, "kind": "click", "ref": "e12"}),
            ],
        )

    def test_response_text_never_requires_browseros_response_shape(self):
        self.assertEqual(
            bridge.response_text([{"text": "opened page 3"}]), "opened page 3"
        )
        self.assertEqual(bridge.page_from_response([{"text": "opened page 3"}]), 3)

    def test_v20_fastmcp_session_returns_content_when_data_is_empty(self):
        result = MagicMock(data=None, content=[MagicMock(text="opened page 3")])
        client = MagicMock()

        async def call_tool(*args, **kwargs):
            return result

        client.call_tool.side_effect = call_tool
        with patch.object(bridge, "Client", return_value=client):
            session = bridge.FastMCPBrowserOSSession("http://127.0.0.1:9010/mcp")
            response = asyncio.run(session.call("tabs", {"action": "new"}))

        self.assertEqual(bridge.response_text(response), "opened page 3")

    def test_v21_statement_control_supports_button_or_link(self):
        self.assertEqual(
            bridge.ref_for_roles(
                '- button "Statement" [expanded] [ref=e8]',
                "Statement",
                ("link", "button"),
            ),
            "e8",
        )

    def test_v22_current_statement_activity_label_and_path(self):
        self.assertEqual(
            bridge.ref_for_names(
                '- link "View Statement Activity" [ref=e93]',
                "link",
                ("Go to Statement Activity", "View Statement Activity"),
            ),
            "e93",
        )
        self.assertTrue(
            bridge.is_activity_page(
                "[UNTRUSTED_PAGE_CONTENT origin=https://global.americanexpress.com/activity/]"
            )
        )
        self.assertFalse(
            bridge.is_activity_page(
                "[UNTRUSTED_PAGE_CONTENT origin=https://global.americanexpress.com/activity/statements]"
            )
        )

    def test_v23_download_control_supports_button_or_link(self):
        self.assertEqual(
            bridge.ref_for_roles(
                '- link "Download" [ref=e44]', "Download", ("button", "link")
            ),
            "e44",
        )

    def test_statement_download_ref_matches_requested_month(self):
        self.assertEqual(
            bridge.statement_ref(
                '- button "Download 28 July 2026 Statement" [ref=e72]',
                "2026-07",
            ),
            "e72",
        )

    def test_statement_month_waits_for_fresh_download_ref(self):
        browser = FakeBrowserOS(
            [
                "[UNTRUSTED_PAGE_CONTENT origin=https://global.americanexpress.com/activity/statements]",
                """[UNTRUSTED_PAGE_CONTENT origin=https://global.americanexpress.com/activity/statements]
- button \"Download 28 July 2026 Statement\" [ref=e72]""",
            ]
        )

        _, ref = asyncio.run(bridge.statement_snapshot_for_month(browser, 3, "2026-07"))

        self.assertEqual(ref, "e72")
        self.assertEqual([call[0] for call in browser.calls], ["snapshot", "snapshot"])

    def test_v24_identifies_authenticated_dashboard(self):
        self.assertTrue(
            bridge.is_authenticated_page(
                "[UNTRUSTED_PAGE_CONTENT origin=https://global.americanexpress.com/dashboard]"
            )
        )
        self.assertFalse(
            bridge.is_authenticated_page(
                "[UNTRUSTED_PAGE_CONTENT origin=https://www.americanexpress.com/en-ca/account/login/]"
            )
        )

    def test_v25_requires_visible_statement_control_for_dashboard_ready(self):
        self.assertFalse(
            bridge.is_dashboard_ready(
                "[UNTRUSTED_PAGE_CONTENT origin=https://global.americanexpress.com/dashboard]"
            )
        )

    def test_v25_navigation_waits_for_statement_after_dashboard_rerender(self):
        browser = FakeBrowserOS(
            [
                "[UNTRUSTED_PAGE_CONTENT origin=https://global.americanexpress.com/dashboard]",
                """[UNTRUSTED_PAGE_CONTENT origin=https://global.americanexpress.com/dashboard]
- button \"Statement\" [ref=e8]""",
                None,
                """[UNTRUSTED_PAGE_CONTENT origin=https://global.americanexpress.com/dashboard]
- link \"Export Statement Data\" [ref=e9]""",
                None,
                """[UNTRUSTED_PAGE_CONTENT origin=https://global.americanexpress.com/activity/statements]
- link \"View Statement Activity\" [ref=e10]""",
                None,
                "[UNTRUSTED_PAGE_CONTENT origin=https://global.americanexpress.com/activity/]",
            ]
        )

        activity = asyncio.run(bridge.navigate_to_activity(browser, 3))

        self.assertTrue(bridge.is_activity_page(activity))
        self.assertTrue(
            bridge.is_dashboard_ready(
                """[UNTRUSTED_PAGE_CONTENT origin=https://global.americanexpress.com/dashboard]
- button \"Statement\" [ref=e8]"""
            )
        )

    def test_v26_login_destination_does_not_count_as_authenticated(self):
        self.assertFalse(
            bridge.is_authenticated_page(
                """[UNTRUSTED_PAGE_CONTENT origin=https://www.americanexpress.com/en-ca/account/login?DestPage=https://global.americanexpress.com/dashboard]"""
            )
        )

    def test_authentication_fails_immediately_for_security_challenge(self):
        login_snapshot = """[UNTRUSTED_PAGE_CONTENT origin=https://www.americanexpress.com/en-ca/account/login/]
- textbox \"User ID\" [ref=e10]
- textbox \"Password\" [ref=e11]
- button \"Log In\" [ref=e12]"""
        challenge_snapshot = "[UNTRUSTED_PAGE_CONTENT origin=https://www.americanexpress.com/verify/] CAPTCHA"
        browser = FakeBrowserOS([login_snapshot, None, None, challenge_snapshot])

        with self.assertRaisesRegex(RuntimeError, "interactive security challenge"):
            asyncio.run(bridge.authenticate(browser, 3, "user", "password"))

    @patch.object(bridge, "wait_for_amex_sms_code", return_value="123456")
    def test_sms_mfa_uses_fields_payload(self, _wait_for_code: MagicMock):
        code_page = """[UNTRUSTED_PAGE_CONTENT origin=https://www.americanexpress.com/login]
- generic \"Please enter the verification code\"
- textbox \"Verification code\" [ref=e3]
- button \"Continue\" [ref=e4]"""
        delivery = """[UNTRUSTED_PAGE_CONTENT origin=https://www.americanexpress.com/login]
- radio \"SMS\" [ref=e1]
- button \"Continue\" [ref=e2]"""
        browser = FakeBrowserOS([None, None, code_page, None, None])

        asyncio.run(bridge.complete_sms_mfa(browser, 3, delivery))

        self.assertEqual(
            browser.calls[3],
            (
                "act",
                {
                    "page": 3,
                    "kind": "fill",
                    "fields": [{"ref": "e3", "value": "123456"}],
                },
            ),
        )

    def test_run_download_validates_and_returns_only_paths(self):
        fixture = Path(__file__).parent / "fixtures" / "activity.csv"
        with tempfile.TemporaryDirectory() as directory:
            downloaded = Path(directory) / "activity.csv"
            downloaded.write_bytes(fixture.read_bytes())
            output_dir = Path(directory) / "output"
            authenticated = """[UNTRUSTED_PAGE_CONTENT origin=https://global.americanexpress.com/dashboard/]
- link \"Statement\" [ref=e13]"""
            activity = """[UNTRUSTED_PAGE_CONTENT origin=https://global.americanexpress.com/activity/]
- button \"Download\" [ref=e20]"""
            dialog = """[UNTRUSTED_PAGE_CONTENT origin=https://global.americanexpress.com/activity/]
- radio \"CSV\" [ref=e21]
- link \"Download\" [ref=e22]"""
            browser = FakeBrowserOS(
                [
                    [{"text": "opened page 3"}],
                    """[UNTRUSTED_PAGE_CONTENT origin=https://www.americanexpress.com/en-ca/account/login/]
- textbox \"User ID\" [ref=e10]
- textbox \"Password\" [ref=e11]
- button \"Log In\" [ref=e12]""",
                    None,
                    None,
                    authenticated,
                    authenticated,
                    authenticated,
                    None,
                    """[UNTRUSTED_PAGE_CONTENT origin=https://global.americanexpress.com/dashboard/]
- link \"Export Statement Data\" [ref=e14]""",
                    None,
                    """[UNTRUSTED_PAGE_CONTENT origin=https://global.americanexpress.com/activity/statements/]
- link \"Go to Statement Activity\" [ref=e15]""",
                    None,
                    "[UNTRUSTED_PAGE_CONTENT origin=https://global.americanexpress.com/activity/]",
                    activity,
                    None,
                    dialog,
                    None,
                    dialog,
                    {"path": str(downloaded)},
                ]
            )
            with patch.dict(
                os.environ,
                {
                    "AMEX_USER": "test-user",
                    "AMEX_PASSWORD": "test-password",
                    "BROWSEROS_MCP_URL": "http://127.0.0.1:9239/mcp",
                    "BROWSEROS_DOWNLOAD_DIR": str(Path(directory)),
                },
                clear=False,
            ):
                result = asyncio.run(
                    bridge.run_download(
                        str(output_dir), session_factory=lambda _: browser
                    )
                )

            self.assertEqual(result["status"], "downloaded")
            self.assertEqual(
                [Path(path).resolve().parent for path in result["paths"]],
                [output_dir.resolve()],
            )
            self.assertEqual(Path(result["paths"][0]).name, "amex-latest.csv")
            self.assertEqual(set(result), {"status", "paths"})
            self.assertNotIn("test-user", str(result))
            self.assertNotIn("test-password", str(result))

    def test_export_latest_waits_for_download_control(self):
        activity = "[UNTRUSTED_PAGE_CONTENT origin=https://global.americanexpress.com/activity/]"
        ready = """[UNTRUSTED_PAGE_CONTENT origin=https://global.americanexpress.com/activity/]
- button \"Download\" [ref=e20]"""
        dialog = """[UNTRUSTED_PAGE_CONTENT origin=https://global.americanexpress.com/activity/]
- radio \"CSV\" [ref=e21]
- link \"Download\" [ref=e22]"""
        browser = FakeBrowserOS(
            [ready, None, dialog, None, dialog, {"path": "/tmp/activity.csv"}]
        )

        with patch.object(
            bridge, "download_path", return_value=Path("/tmp/activity.csv")
        ):
            downloaded = asyncio.run(bridge.export_latest_csv(browser, 3, activity))

        self.assertEqual(downloaded, Path("/tmp/activity.csv"))
        self.assertEqual(browser.calls[0], ("snapshot", {"page": 3}))

    def test_missing_credentials_are_actionable_without_values(self):
        with patch.dict(os.environ, {}, clear=True):
            with self.assertRaisesRegex(RuntimeError, "AMEX_USER and AMEX_PASSWORD"):
                bridge.credentials()


if __name__ == "__main__":
    unittest.main()

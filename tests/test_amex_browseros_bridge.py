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


class RepeatingBrowserOS(FakeBrowserOS):
    """FakeBrowserOS that answers every call with the same response."""

    def __init__(self, response):
        super().__init__([])
        self.response = response

    async def call(self, tool_name, arguments):
        self.calls.append((tool_name, arguments))
        return self.response


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

    def test_statement_archive_ref_finds_collapsed_previous_statements(self):
        self.assertEqual(
            bridge.statement_archive_ref(
                '- button "Previous Statements" [collapsed] [ref=e76]'
            ),
            "e76",
        )

    def test_statement_archive_ref_absent_when_section_missing(self):
        self.assertIsNone(
            bridge.statement_archive_ref(
                '- button "Recent Statements" [expanded] [ref=e62]'
            )
        )

    def test_statement_archive_ref_ignores_already_expanded_section(self):
        """Clicking an expanded toggle would collapse it and hide the months."""
        self.assertIsNone(
            bridge.statement_archive_ref(
                '- button "Previous Statements" [expanded] [ref=e76]'
            )
        )

    def test_expands_previous_statements_to_reach_archived_month(self):
        """
        A month older than the recent window is only reachable after the
        collapsed archive section is expanded.
        """
        browser = FakeBrowserOS(
            [
                """[UNTRUSTED_PAGE_CONTENT origin=https://global.americanexpress.com/activity/statements]
- button "Previous Statements" [collapsed] [ref=e76]
- button "Download 28 March 2026 Statement" [ref=e74]""",
                "clicked",
                """[UNTRUSTED_PAGE_CONTENT origin=https://global.americanexpress.com/activity/statements]
- button "Previous Statements" [expanded] [ref=e76]
- button "Download 28 February 2026 Statement" [ref=e99]""",
            ]
        )

        _, ref = asyncio.run(bridge.statement_snapshot_for_month(browser, 3, "2026-02"))

        self.assertEqual(ref, "e99")
        self.assertEqual(
            [(call[0], call[1].get("kind")) for call in browser.calls],
            [("snapshot", None), ("act", "click"), ("snapshot", None)],
        )
        self.assertEqual(browser.calls[1][1]["ref"], "e76")

    def test_expands_archive_only_once_across_polls(self):
        """The toggle must not be clicked repeatedly while the month is absent."""
        browser = FakeBrowserOS(
            [
                """[UNTRUSTED_PAGE_CONTENT origin=https://global.americanexpress.com/activity/statements]
- button "Previous Statements" [collapsed] [ref=e76]""",
                "clicked",
                """[UNTRUSTED_PAGE_CONTENT origin=https://global.americanexpress.com/activity/statements]
- button "Previous Statements" [collapsed] [ref=e76]""",
                """[UNTRUSTED_PAGE_CONTENT origin=https://global.americanexpress.com/activity/statements]
- button "Download 28 February 2026 Statement" [ref=e99]""",
            ]
        )

        _, ref = asyncio.run(bridge.statement_snapshot_for_month(browser, 3, "2026-02"))

        self.assertEqual(ref, "e99")
        clicks = [c for c in browser.calls if c[0] == "act"]
        self.assertEqual(len(clicks), 1)

    def test_no_archive_click_when_month_is_already_visible(self):
        browser = FakeBrowserOS(
            [
                """[UNTRUSTED_PAGE_CONTENT origin=https://global.americanexpress.com/activity/statements]
- button "Previous Statements" [collapsed] [ref=e76]
- button "Download 28 February 2026 Statement" [ref=e99]""",
            ]
        )

        _, ref = asyncio.run(bridge.statement_snapshot_for_month(browser, 3, "2026-02"))

        self.assertEqual(ref, "e99")
        self.assertEqual([call[0] for call in browser.calls], ["snapshot"])

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
                    "[UNTRUSTED_PAGE_CONTENT origin=https://global.americanexpress.com/activity/]",
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
                        str(output_dir), session_factory=lambda endpoint: browser
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
        closed = "[UNTRUSTED_PAGE_CONTENT origin=https://global.americanexpress.com/activity/]"
        browser = FakeBrowserOS(
            [ready, None, dialog, None, dialog, {"path": "/tmp/activity.csv"}, closed]
        )

        with patch.object(
            bridge, "download_path", return_value=Path("/tmp/activity.csv")
        ):
            downloaded = asyncio.run(bridge.export_latest_csv(browser, 3, activity))

        self.assertEqual(downloaded, Path("/tmp/activity.csv"))
        self.assertEqual(browser.calls[0], ("snapshot", {"page": 3}))

    def test_export_waits_for_export_dialog_to_close(self):
        """
        The export dialog is a full-viewport overlay; returning while it is
        still up leaves the next statement's download button unclickable.
        """
        dialog = """[UNTRUSTED_PAGE_CONTENT origin=https://global.americanexpress.com/activity/statements/]
- radio \"CSV\" [ref=e21]
- link \"Download\" [ref=e22]"""
        closed = "[UNTRUSTED_PAGE_CONTENT origin=https://global.americanexpress.com/activity/statements/]"
        browser = FakeBrowserOS(
            [
                None,
                dialog,
                None,
                dialog,
                {"path": "/tmp/activity.csv"},
                dialog,
                closed,
            ]
        )

        with patch.object(
            bridge, "download_path", return_value=Path("/tmp/activity.csv")
        ):
            downloaded = asyncio.run(bridge.export_csv(browser, 3, closed, ref="e70"))

        self.assertEqual(downloaded, Path("/tmp/activity.csv"))
        self.assertEqual(
            [call[0] for call in browser.calls],
            ["act", "snapshot", "act", "snapshot", "download", "snapshot", "snapshot"],
        )

    @patch.object(bridge, "ACTION_TIMEOUT_SECONDS", 0.05)
    def test_export_fails_when_export_dialog_never_closes(self):
        dialog = """[UNTRUSTED_PAGE_CONTENT origin=https://global.americanexpress.com/activity/statements/]
- radio \"CSV\" [ref=e21]
- link \"Download\" [ref=e22]"""
        browser = RepeatingBrowserOS(dialog)

        with patch.object(
            bridge, "download_path", return_value=Path("/tmp/activity.csv")
        ):
            with self.assertRaisesRegex(RuntimeError, "export dialog to close"):
                asyncio.run(bridge.export_csv(browser, 3, dialog, ref="e70"))

    def test_export_statement_retries_when_previous_dialog_still_covers_button(self):
        """
        The export dialog can outlive its own download, leaving the next
        month's button covered until the overlay finishes fading out.
        """
        statements = """[UNTRUSTED_PAGE_CONTENT origin=https://global.americanexpress.com/activity/statements/]
- button \"Download 28 August 2026 Statement\" [ref=e64]"""
        dialog = """[UNTRUSTED_PAGE_CONTENT origin=https://global.americanexpress.com/activity/statements/]
- radio \"CSV\" [ref=e21]
- link \"Download\" [ref=e22]"""
        closed = "[UNTRUSTED_PAGE_CONTENT origin=https://global.americanexpress.com/activity/statements/]"
        covered = (
            'act failed: Element e64 (button "Download 28 August 2026 Statement") '
            "is covered by <button.flex> at its click point"
        )

        class CoveringOnce(FakeBrowserOS):
            def __init__(self, responses):
                super().__init__(responses)
                self.clicks = 0

            async def call(self, tool_name, arguments):
                if tool_name == "act" and arguments.get("kind") == "click":
                    self.clicks += 1
                    if self.clicks == 1:
                        self.calls.append((tool_name, arguments))
                        raise RuntimeError(covered)
                return await super().call(tool_name, arguments)

        browser = CoveringOnce(
            [
                statements,
                statements,
                None,
                dialog,
                None,
                dialog,
                {"path": "/tmp/a.csv"},
                closed,
            ]
        )

        with patch.object(bridge, "download_path", return_value=Path("/tmp/a.csv")):
            downloaded = asyncio.run(bridge.export_statement_csv(browser, 3, "2026-08"))

        self.assertEqual(downloaded, Path("/tmp/a.csv"))
        self.assertGreaterEqual(browser.clicks, 2)

    def test_export_statement_propagates_unrelated_click_failure(self):
        statements = """[UNTRUSTED_PAGE_CONTENT origin=https://global.americanexpress.com/activity/statements/]
- button \"Download 28 August 2026 Statement\" [ref=e64]"""

        class AlwaysFailing(FakeBrowserOS):
            async def call(self, tool_name, arguments):
                if tool_name == "act":
                    raise RuntimeError("Amex page changed: no Download")
                return await super().call(tool_name, arguments)

        browser = AlwaysFailing([statements])

        with self.assertRaisesRegex(RuntimeError, "no Download"):
            asyncio.run(bridge.export_statement_csv(browser, 3, "2026-08"))

    def test_missing_credentials_are_actionable_without_values(self):
        with patch.dict(os.environ, {}, clear=True):
            with self.assertRaisesRegex(RuntimeError, "AMEX_USER and AMEX_PASSWORD"):
                bridge.credentials()


if __name__ == "__main__":
    unittest.main()

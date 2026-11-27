import asyncio
import os
import tempfile
import unittest
from datetime import date
from pathlib import Path
from unittest.mock import patch

from scripts import td_browseros_bridge as bridge


def page(url: str, body: str) -> str:
    return f"[UNTRUSTED_PAGE_CONTENT nonce=1 origin={url}] data\n{body}\n[END]"


class ScriptedTDBrowserOS:
    def __init__(self, pages, *, tabs_reply=None, download_reply=None):
        self.pages = list(pages)
        self.tabs_reply = (
            tabs_reply
            if tabs_reply is not None
            else {"page": 12, "contexts": [{"page": 88}]}
        )
        self.download_reply = download_reply
        self.calls = []
        self.timeouts = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return None

    async def call(self, tool_name, arguments, *, timeout=None):
        self.calls.append((tool_name, arguments))
        self.timeouts.append(timeout)
        if tool_name == "tabs":
            return self.tabs_reply
        if tool_name == "snapshot":
            return self.pages.pop(0) if len(self.pages) > 1 else self.pages[0]
        if tool_name == "download":
            return self.download_reply
        return {"changed": False, "kind": arguments.get("kind")}


class TestTDBrowserOSBridge(unittest.TestCase):
    def test_v8_td_structured_tabs_opaque_act_and_download_timeout(self):
        with tempfile.TemporaryDirectory() as directory:
            downloaded = Path(directory) / "accountactivity.csv"
            downloaded.write_text("2026-10-05,Shop,10.00,,100.00\n")
            accounts = page(
                "https://easyweb.td.com/accounts",
                '- link "Logout" [ref=e1]\n- link "Joint C$ Cheq A/c" [ref=e2]',
            )
            cheq = page(
                "https://easyweb.td.com/accounts/cheq",
                '- combobox "Quick filters" [ref=e3]: "Last 30 days"\n'
                '- button "Filter" [ref=e4]\n'
                '- button "Download" [ref=e5]',
            )
            filter_panel = page(
                "https://easyweb.td.com/accounts/cheq",
                '- textbox "From" [ref=e6]\n'
                '- textbox "To" [ref=e7]\n'
                '- button "Apply filters" [ref=e8]',
            )
            filtered = page(
                "https://easyweb.td.com/accounts/cheq",
                '- combobox "Quick filters" [ref=e3]: "Custom"\n'
                '- cell "Oct 5, 2026"\n'
                '- button "Download" [ref=e5]',
            )
            dialog = page(
                "https://easyweb.td.com/accounts/cheq",
                '- dialog "Download Your Transaction Activity"\n'
                '- combobox "Select Download Format" [ref=e9]: "Spreadsheet (.CSV)"\n'
                '- button "Download" [ref=e10]',
            )
            browser = ScriptedTDBrowserOS(
                [accounts, accounts, cheq, filter_panel, filtered, dialog, accounts],
                download_reply={"path": str(downloaded), "page": 12},
            )
            with patch.dict(
                os.environ,
                {
                    "BROWSEROS_MCP_URL": "http://127.0.0.1:9010/mcp",
                    "BROWSEROS_DOWNLOAD_DIR": directory,
                },
            ):
                files = asyncio.run(
                    bridge.run_download(
                        "debit",
                        date(2026, 10, 1),
                        date(2026, 10, 31),
                        "user",
                        "pass",
                        session_factory=lambda _: browser,
                    )
                )
            try:
                self.assertEqual(len(files), 1)
                self.assertTrue(files[0].exists())
            finally:
                for f in files:
                    f.unlink(missing_ok=True)
                    f.parent.rmdir()

            download_timeouts = [
                timeout
                for (name, _), timeout in zip(browser.calls, browser.timeouts)
                if name == "download"
            ]
            self.assertEqual(download_timeouts, [180])
            self.assertIn(("tabs", {"action": "close", "page": 12}), browser.calls)

    def test_v1_td_malformed_tabs_fails_without_cleanup(self):
        browser = ScriptedTDBrowserOS(
            [], tabs_reply={"contexts": [{"page": 9, "text": "opened page 9"}]}
        )
        with self.assertRaises(RuntimeError):
            asyncio.run(
                bridge.run_download(
                    "debit",
                    date(2026, 10, 1),
                    date(2026, 10, 31),
                    "user",
                    "pass",
                    session_factory=lambda _: browser,
                )
            )
        self.assertEqual([name for name, _ in browser.calls], ["tabs"])


if __name__ == "__main__":
    unittest.main()

import asyncio
import os
import tempfile
import unittest
from datetime import date
from pathlib import Path
from unittest.mock import patch

from scripts import rogers_browseros_bridge as bridge


def page(url: str, body: str) -> str:
    return f"[UNTRUSTED_PAGE_CONTENT nonce=1 origin={url}] data\n{body}\n[END]"


class ScriptedRogersBrowserOS:
    def __init__(self, pages, *, tabs_reply=None, download_reply=None):
        self.pages = list(pages)
        self.tabs_reply = (
            tabs_reply
            if tabs_reply is not None
            else {"page": 11, "contexts": [{"page": 99}]}
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
        if tool_name == "navigate":
            return {"page": arguments.get("page"), "changed": True}
        return {"changed": False, "kind": arguments.get("kind")}


class TestRogersBrowserOSBridge(unittest.TestCase):
    def test_v8_rogers_structured_tabs_opaque_act_and_download_timeout(self):
        with tempfile.TemporaryDirectory() as directory:
            downloaded = Path(directory) / "transactions.csv"
            downloaded.write_text("Date,Description,Amount\n2026-10-05,Shop,10.00\n")
            authed = page(
                "https://selfserve.rogersbank.com/home",
                '- button "Sign Out" [ref=e1]',
            )
            txns_page = page(
                bridge.TRANSACTIONS_URL,
                '- generic [ref=e2]\n  - paragraph "Download transactions"',
            )
            dialog = page(
                bridge.TRANSACTIONS_URL,
                '- combobox "Select Statement Period" [ref=e3]: "Oct 2026"\n'
                '- option "Oct 2026" [ref=e4]\n'
                '- button "Download" [ref=e5]',
            )
            browser = ScriptedRogersBrowserOS(
                [authed, txns_page, dialog, dialog, dialog, authed],
                download_reply={"path": str(downloaded), "page": 11},
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
            self.assertIn(("tabs", {"action": "close", "page": 11}), browser.calls)

    def test_v1_rogers_malformed_tabs_fails_without_cleanup(self):
        browser = ScriptedRogersBrowserOS(
            [], tabs_reply={"contexts": [{"page": 9, "text": "opened page 9"}]}
        )
        with self.assertRaises(RuntimeError):
            asyncio.run(
                bridge.run_download(
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

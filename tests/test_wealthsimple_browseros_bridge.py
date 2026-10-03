import asyncio
import json
import os
import tempfile
import unittest
from datetime import date
from pathlib import Path
from unittest.mock import AsyncMock, patch

from scripts import wealthsimple_browseros_bridge as bridge

TARGET = "https://my.wealthsimple.com/app/activity?account_ids=ca-cash-msb-abc"
LOGIN_URL = "https://my.wealthsimple.com/app/login?redirect=activity"


def page(url, body):
    return f"[UNTRUSTED_PAGE_CONTENT nonce=1 origin={url}] data\n{body}\n[END]"


LOGIN = page(
    LOGIN_URL,
    '- textbox "Email" [required] [ref=e6]\n'
    '- textbox "Password" [required] [ref=e8]\n'
    '- button "Log in" [ref=e11]\n'
    '- button "Log in with a passkey" [ref=e12]',
)
VERIFYING = page(LOGIN_URL, '- heading "Verifying that it’s you…" [level=1]')
PASSKEY = page(
    LOGIN_URL,
    '- heading "Use your passkey to verify that it’s you" [level=1]\n'
    '- button "Try another way" [ref=e18]\n'
    '- button "Verify with passkey" [ref=e19]',
)
AUTHENTICATOR = page(
    LOGIN_URL,
    '- heading "Check your authenticator" [level=1]\n'
    '- textbox "Enter your code" [ref=e20]\n'
    '- button "Submit" [disabled] [ref=e21]\n'
    '- checkbox "Don’t ask me for a code for the next 30 days" [ref=e23]',
)
ACTIVITY = page(TARGET, '- heading "Activity" [level=1]')
HOME = page("https://my.wealthsimple.com/app/home", '- heading "Home" [level=1]')


class ScriptedBrowserOS:
    """Answers snapshots from a script, repeating the last page."""

    def __init__(self, pages, rows=None):
        self.pages = list(pages)
        self.rows = list(rows or [])
        self.calls = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return None

    async def call(self, tool_name, arguments):
        self.calls.append((tool_name, arguments))
        if tool_name == "snapshot":
            return self.pages.pop(0) if len(self.pages) > 1 else self.pages[0]
        if tool_name == "evaluate":
            state = self.rows.pop(0) if len(self.rows) > 1 else self.rows[0]
            return page(TARGET, json.dumps(state))
        if tool_name == "tabs":
            return "opened page 7"
        return "ok"

    def fills(self):
        return [
            field
            for name, args in self.calls
            if name == "act" and args["kind"] == "fill"
            for field in args["fields"]
        ]

    def clicks(self):
        return [
            args["ref"]
            for name, args in self.calls
            if name == "act" and args["kind"] == "click"
        ]


def authenticate(fake, **kwargs):
    return asyncio.run(
        bridge.authenticate(
            fake,
            7,
            TARGET,
            "me@example.com",
            "pw",
            "GEZDGNBVGY3TQOJQ",
            poll_seconds=0,
            **kwargs,
        )
    )


@patch.object(bridge.asyncio, "sleep", AsyncMock())
class TestAuthenticate(unittest.TestCase):
    @patch.object(bridge.time, "time", return_value=1_000_000_005.0)
    def test_logs_in_through_try_another_way_and_totp(self, _time):
        fake = ScriptedBrowserOS(
            [
                LOGIN,
                VERIFYING,
                VERIFYING,
                PASSKEY,
                AUTHENTICATOR,
                AUTHENTICATOR,
                ACTIVITY,
            ]
        )

        authenticate(fake)

        fills = fake.fills()
        self.assertEqual(fills[0], {"ref": "e6", "value": "me@example.com"})
        self.assertEqual(fills[1], {"ref": "e8", "value": "pw"})
        self.assertEqual(
            fills[2],
            {
                "ref": "e20",
                "value": bridge.totp_code("GEZDGNBVGY3TQOJQ", 1_000_000_005.0),
            },
        )
        self.assertEqual(fake.clicks(), ["e11", "e18"])
        self.assertNotIn("e23", fake.clicks())

    def test_already_logged_in_sends_no_credentials(self):
        fake = ScriptedBrowserOS([ACTIVITY])

        authenticate(fake)

        self.assertEqual(fake.fills(), [])

    def test_logged_in_on_home_navigates_to_account_activity(self):
        fake = ScriptedBrowserOS([HOME, ACTIVITY])

        authenticate(fake)

        self.assertIn(("navigate", {"page": 7, "url": TARGET}), fake.calls)
        self.assertEqual(fake.fills(), [])

    def test_login_redirect_to_home_navigates_to_account_activity(self):
        fake = ScriptedBrowserOS([LOGIN, AUTHENTICATOR, AUTHENTICATOR, HOME, ACTIVITY])

        authenticate(fake)

        navigations = [args for name, args in fake.calls if name == "navigate"]
        self.assertEqual(navigations, [{"page": 7, "url": TARGET}])

    def test_activity_page_that_never_opens_exits(self):
        fake = ScriptedBrowserOS([HOME])

        with self.assertRaisesRegex(RuntimeError, "did not open the account activity"):
            authenticate(fake)

        navigations = [args for name, args in fake.calls if name == "navigate"]
        self.assertEqual(len(navigations), 2)

    def test_refuses_to_fill_outside_wealthsimple(self):
        for host in (
            "example.com",
            "evilwealthsimple.com",
            "wealthsimple.com.attacker.test",
        ):
            fake = ScriptedBrowserOS([LOGIN.replace("my.wealthsimple.com", host)])
            with self.assertRaisesRegex(RuntimeError, "outside wealthsimple.com"):
                authenticate(fake)
            self.assertEqual(fake.fills(), [])

    def test_other_challenge_exits(self):
        sms = page(
            LOGIN_URL, '- heading "We sent a text message to your phone" [level=1]'
        )
        fake = ScriptedBrowserOS([LOGIN, sms])

        with self.assertRaisesRegex(RuntimeError, "interactive security challenge"):
            authenticate(fake)

    def test_unchanging_unknown_screen_times_out_as_challenge(self):
        fake = ScriptedBrowserOS([LOGIN, VERIFYING])

        with self.assertRaisesRegex(RuntimeError, "interactive security challenge"):
            authenticate(fake, timeout=0.05)

    def test_rejected_totp_retries_next_step_once_then_exits(self):
        clock = iter(float(1_000_000_005 + 30 * n) for n in range(100))
        fake = ScriptedBrowserOS([LOGIN, AUTHENTICATOR])

        with patch.object(bridge.time, "time", side_effect=lambda: next(clock)):
            with self.assertRaisesRegex(RuntimeError, "TOTP code twice"):
                authenticate(fake)

        code_fills = [f for f in fake.fills() if f["ref"] == "e20"]
        self.assertEqual(len(code_fills), 2)

    def test_secrets_only_travel_in_fill_values(self):
        fake = ScriptedBrowserOS([LOGIN, AUTHENTICATOR, ACTIVITY])

        authenticate(fake)

        code = bridge.totp_code("GEZDGNBVGY3TQOJQ")
        for name, args in fake.calls:
            if name == "act" and args["kind"] == "fill":
                continue
            text = json.dumps(args)
            for secret in ("pw", "GEZDGNBVGY3TQOJQ", code):
                self.assertNotIn(f'"{secret}"', text)


class TestTotp(unittest.TestCase):
    # RFC 6238 appendix B, SHA-1 seed "12345678901234567890", last 6 digits.
    SECRET = "GEZDGNBVGY3TQOJQGEZDGNBVGY3TQOJQ"

    def test_rfc6238_vectors(self):
        for at, expected in (
            (59, "287082"),
            (1111111109, "081804"),
            (1234567890, "005924"),
        ):
            self.assertEqual(bridge.totp_code(self.SECRET, at), expected)

    def test_ignores_spaces_and_case(self):
        self.assertEqual(
            bridge.totp_code("gezd gnbv gy3t qojq gezd gnbv gy3t qojq", 59), "287082"
        )


def rows(*items, load_more=True):
    return {
        "rows": [{"heading": d, "date": d, "spans": list(spans)} for d, spans in items],
        "loadMore": load_more,
    }


@patch.object(bridge.asyncio, "sleep", AsyncMock())
class TestLoadUntil(unittest.TestCase):
    MORE = page(TARGET, '- button "Load more" [ref=e84]')
    ROW = ("Shop", "Purchase", "Wealthsimple credit card", "− $1.00 CAD")

    def test_clicks_load_more_until_older_than_since(self):
        first = rows(("September 28, 2026", self.ROW))
        second = rows(("September 28, 2026", self.ROW), ("August 1, 2026", self.ROW))
        fake = ScriptedBrowserOS([self.MORE], [first, second])

        loaded = asyncio.run(
            bridge.load_until(fake, 7, date(2026, 9, 1), poll_seconds=0)
        )

        self.assertEqual(len(loaded), 2)
        self.assertEqual(fake.clicks(), ["e84"])

    def test_stops_when_load_more_is_gone(self):
        fake = ScriptedBrowserOS(
            [self.MORE], [rows(("September 28, 2026", self.ROW), load_more=False)]
        )

        asyncio.run(bridge.load_until(fake, 7, date(2020, 1, 1), poll_seconds=0))

        self.assertEqual(fake.clicks(), [])

    def test_click_cap_exits(self):
        growing = [rows(*[("September 28, 2026", self.ROW)] * n) for n in range(1, 10)]
        fake = ScriptedBrowserOS([self.MORE], growing)

        with self.assertRaisesRegex(RuntimeError, "3 Load more clicks"):
            asyncio.run(
                bridge.load_until(
                    fake, 7, date(2020, 1, 1), max_clicks=3, poll_seconds=0
                )
            )

    def test_waits_for_list_to_render_before_reading(self):
        empty = rows(load_more=False)
        loaded = rows(("September 28, 2026", self.ROW), load_more=False)
        fake = ScriptedBrowserOS([self.MORE], [empty, empty, loaded])

        result = asyncio.run(
            bridge.load_until(fake, 7, date(2026, 9, 1), poll_seconds=0)
        )

        self.assertEqual(len(result), 1)

    def test_list_that_never_renders_exits(self):
        fake = ScriptedBrowserOS([self.MORE], [rows(load_more=False)])

        with self.assertRaisesRegex(RuntimeError, "activity to load"):
            asyncio.run(
                bridge.load_until(
                    fake, 7, date(2026, 9, 1), timeout=0.05, poll_seconds=0
                )
            )


class TestTransactions(unittest.TestCase):
    def test_maps_spans_and_drops_rows_before_since(self):
        state = rows(
            (
                "September 28, 2026",
                ("AMEX", "Pre-authorized debit", "Chequing • Hot", "− $5.00 CAD"),
            ),
            ("September 28, 2026", ("Transfer out", "Chequing • Hot", "− $2.00 CAD")),
            ("August 31, 2026", ("Old", "Purchase", "Card", "− $1.00 CAD")),
        )
        state["rows"].append(
            {
                "heading": "Pending",
                "date": None,
                "spans": ["Pend", "Purchase", "Card", "− $9.00 CAD"],
            }
        )

        result = bridge.transactions(state["rows"], date(2026, 9, 1))

        self.assertEqual(
            result,
            [
                {
                    "description": "AMEX",
                    "type": "Pre-authorized debit",
                    "amount": "− $5.00 CAD",
                    "date": "2026-09-28",
                },
                {
                    "description": "Transfer out",
                    "type": "Chequing • Hot",
                    "amount": "− $2.00 CAD",
                    "date": "2026-09-28",
                },
            ],
        )

    def test_non_cad_row_exits(self):
        state = rows(("September 28, 2026", ("Shop", "Purchase", "Card", "$5.00 USD")))
        with self.assertRaisesRegex(RuntimeError, "non-CAD"):
            bridge.transactions(state["rows"], date(2026, 9, 1))


class TestWriteCsv(unittest.TestCase):
    ROW = {
        "description": "Shop",
        "type": "Purchase",
        "amount": "− $1.00 CAD",
        "date": "2026-09-28",
    }

    def test_writes_header_and_rows(self):
        with tempfile.TemporaryDirectory() as directory:
            saved = bridge.write_csv([self.ROW], Path(directory) / "ws.csv")
            self.assertEqual(
                saved.read_text(encoding="utf-8").splitlines(),
                [
                    "description,type,amount,date",
                    "Shop,Purchase,− $1.00 CAD,2026-09-28",
                ],
            )
            self.assertEqual(sorted(os.listdir(directory)), ["ws.csv"])

    def test_refuses_overwrite_and_leaves_no_partial_file(self):
        with tempfile.TemporaryDirectory() as directory:
            existing = Path(directory) / "ws.csv"
            existing.write_bytes(b"keep")
            with self.assertRaises(FileExistsError):
                bridge.write_csv([self.ROW], existing)
            self.assertEqual(existing.read_bytes(), b"keep")
            self.assertEqual(os.listdir(directory), ["ws.csv"])


class TestRunScrape(unittest.TestCase):
    @patch.object(bridge.asyncio, "sleep", AsyncMock())
    def test_closes_tab_when_login_fails(self):
        fake = ScriptedBrowserOS([page("https://example.com/login", "")])

        with self.assertRaises(RuntimeError):
            asyncio.run(
                bridge.run_scrape(
                    "app/activity?account_ids=x",
                    date(2026, 9, 1),
                    "e",
                    "p",
                    "S",
                    session_factory=lambda endpoint: fake,
                )
            )

        self.assertIn(("tabs", {"action": "close", "page": 7}), fake.calls)


if __name__ == "__main__":
    unittest.main()

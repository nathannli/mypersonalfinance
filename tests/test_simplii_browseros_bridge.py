import asyncio
import os
import tempfile
import unittest
from datetime import date
from pathlib import Path
from unittest.mock import patch

from scripts import simplii_browseros_bridge as bridge

SIGNON_URL = (
    "https://online.simplii.com/ebm-resources/public/simplii/online-banking"
    "/client/index.html#/auth/signon"
)
ACCOUNTS_URL = (
    "https://online.simplii.com/ebm-resources/public/simplii/online-banking"
    "/accounts/client/index.html#/accounts"
)
DOWNLOAD_URL = (
    "https://online.simplii.com/ebm-resources/public/client/web/index.html"
    "#/accounts/download"
)

CHEQUING = "No Fee Chequing Account (0075943282) $200.00"
CARD = "Cash Back Visa* Card (4525 5010 2341 4621) $12.34"
OTHER_CHEQUING = "Chequing Account (0075949999) $5.00"


def page(url, body):
    return f"[UNTRUSTED_PAGE_CONTENT nonce=1 origin={url}] data\n{body}\n[END]"


def download_form(
    *,
    options=(CHEQUING, CARD),
    account=CHEQUING,
    since=("September", "1", "2026"),
    until=("October", "10", "2026"),
    fmt="Choose your software",
):
    body = [f'- combobox "Account:" [collapsed] [ref=e38]: "{account}"']
    body += [
        f'- option "{option}" [ref=e4{index}]' for index, option in enumerate(options)
    ]
    body += [
        '- radio "All since last download" [ref=e48]',
        '- generic "All within date range" [ref=e49]',
        '- radio "All within date range" [ref=e50]',
        f'- combobox "From month" [collapsed] [ref=e51]: "{since[0]}"',
        '- option "September" [ref=e52]',
        '- option "October" [ref=e53]',
        f'- combobox "From day" [collapsed] [ref=e64]: "{since[1]}"',
        '- option "1" [ref=e65]',
        f'- combobox "From year" [collapsed] [ref=e96]: "{since[2]}"',
        '- option "2026" [ref=e97]',
        f'- combobox "To month" [collapsed] [ref=e113]: "{until[0]}"',
        '- option "October" [ref=e114]',
        f'- combobox "To day" [collapsed] [ref=e126]: "{until[1]}"',
        '- option "10" [ref=e127]',
        f'- combobox "To year" [collapsed] [ref=e158]: "{until[2]}"',
        '- option "2026" [ref=e159]',
        f'- combobox "Financial management software:" [collapsed] [ref=e175]: "{fmt}"',
        '- option "Spreadsheet (CSV)" [ref=e179]',
        '- button "Download Transactions" [ref=e180]',
    ]
    return page(DOWNLOAD_URL, "\n".join(body))


SIGNON = page(
    SIGNON_URL,
    '- textbox "Card number" [ref=e3]\n'
    '- textbox "Password (case sensitive)" [ref=e6]\n'
    '- button "Sign in." [ref=e9]',
)
SIGNON_PREFILLED = page(
    SIGNON_URL,
    '- textbox "Card number" [ref=e3]: "1111 2222 3333 4444"\n'
    '- textbox "Password (case sensitive)" [ref=e6]\n'
    '- button "Sign in." [ref=e9]',
)
ACCOUNTS = page(
    ACCOUNTS_URL,
    '- link "Sign out" [ref=e4]\n'
    '- menuitem "Download Transactions" [ref=e29]\n'
    '- link "No Fee Chequing Account . Account number 0075943282" [ref=e37]\n'
    '- link "Cash Back Visa* Card . Account number 4525 5010 2341 4621" [ref=e60]',
)
SIGNON_WITH_CAPTCHA = page(
    SIGNON_URL, '- heading "Please complete the CAPTCHA" [level=1]'
)


def verification_screen(options, chosen):
    """The real Verify your identity screen and its contact-preference chooser."""
    body = ['- heading "Verify your identity" [level=2]']
    body += [
        f'- combobox "Your contact preference" [collapsed] [ref=e2]: "{chosen}"',
        '- option "Select" [ref=e3]',
    ]
    body += [
        f'- option "{option}" [ref=e{4 + index}]'
        for index, option in enumerate(options)
    ]
    body += ['- button "Send" [ref=e7]', '- button "Cancel" [ref=e8]']
    return page(SIGNON_URL, "\n".join(body))


VERIFICATION_PUSH_ONLY = verification_screen(
    ("Push to your device ********", "Outbound call 64X-XXX-X162"),
    "Push to your device ********",
)
VERIFICATION_WITH_SMS = verification_screen(
    (
        "Push to your device ********",
        "SMS 64X-XXX-X162",
        "Outbound call 64X-XXX-X162",
    ),
    "Push to your device ********",
)
IDENTITY_TEXT_IN_PAGE = page(
    ACCOUNTS_URL,
    '- link "Sign out" [ref=e4]\n- link "How we verify your identity" [ref=e9]',
)
CODE_PAGE = page(
    SIGNON_URL,
    '- textbox "Verification code" [ref=e40]\n- button "Continue" [ref=e41]',
)
OPAQUE = page(SIGNON_URL, '- heading "Please wait" [level=1]')


def applied_form(**overrides):
    """The export form as it reads back once the bridge has set it up."""
    overrides.setdefault("fmt", "Spreadsheet (CSV)")
    return download_form(**overrides)


FILLED = applied_form(account=CARD)


class ScriptedBrowserOS:
    """Answers snapshots from a script, repeating the last page."""

    def __init__(self, pages, *, download=None):
        self.pages = list(pages)
        self.download = download
        self.calls = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return None

    async def call(self, tool_name, arguments):
        self.calls.append((tool_name, arguments))
        if tool_name == "tabs":
            return "opened page 7"
        if tool_name == "snapshot":
            return self.pages.pop(0) if len(self.pages) > 1 else self.pages[0]
        if tool_name == "download":
            return self.download
        return "ok"

    def acts(self, kind=None, **match):
        found = []
        for name, arguments in self.calls:
            if name != "act" or (kind is not None and arguments["kind"] != kind):
                continue
            if all(arguments.get(key) == value for key, value in match.items()):
                found.append(arguments)
        return found

    def ops(self):
        return [
            (name, arguments.get("kind"), arguments.get("ref") or arguments.get("text"))
            for name, arguments in self.calls
            if name == "act"
        ]

    def selects(self):
        return [arguments for arguments in self.acts("select")]

    def fills(self):
        return [
            field for arguments in self.acts("fill") for field in arguments["fields"]
        ]


class TestAuthenticate(unittest.TestCase):
    def authenticate(self, pages, **kwargs):
        browser = ScriptedBrowserOS(pages)
        asyncio.run(bridge.authenticate(browser, 7, "user-value", "password", **kwargs))
        return browser

    def test_reuses_an_authenticated_session_without_credentials(self):
        browser = self.authenticate([ACCOUNTS])
        self.assertEqual(browser.ops(), [])

    def test_signs_in_on_simplii_with_the_configured_credentials(self):
        browser = self.authenticate([SIGNON, ACCOUNTS])
        self.assertEqual(
            browser.ops(),
            [
                ("act", "focus", "e3"),
                ("act", "type", "user-value"),
                ("act", "fill", None),
                ("act", "click", "e9"),
            ],
        )
        # The masking card field rejects `fill`, so the card number must travel
        # as keystrokes and the password as a verified fill value.
        self.assertEqual(browser.fills(), [{"ref": "e6", "value": "password"}])

    def test_clears_a_prefilled_card_number_before_typing(self):
        browser = self.authenticate([SIGNON_PREFILLED, ACCOUNTS])
        self.assertEqual(browser.ops()[0], ("act", "fill", None))
        self.assertEqual(browser.fills()[0], {"ref": "e3", "value": ""})
        self.assertIn(("act", "type", "user-value"), browser.ops())

    def test_refuses_to_fill_outside_simplii(self):
        impostor = page(
            "https://online.simplii.com.evil.test/signon",
            '- textbox "Card number" [ref=e3]\n'
            '- textbox "Password (case sensitive)" [ref=e6]\n'
            '- button "Sign in." [ref=e9]',
        )
        browser = ScriptedBrowserOS([impostor])
        with self.assertRaisesRegex(RuntimeError, "outside simplii.com"):
            asyncio.run(bridge.authenticate(browser, 7, "user-value", "password"))
        self.assertEqual(browser.ops(), [])

    def test_captcha_stops_the_run_without_sending_credentials(self):
        browser = ScriptedBrowserOS([SIGNON_WITH_CAPTCHA])
        with self.assertRaises(bridge.InteractiveChallengeError):
            asyncio.run(bridge.authenticate(browser, 7, "user-value", "password"))
        self.assertEqual(browser.ops(), [])

    def test_an_unanswerable_step_up_waits_for_the_operator(self):
        # The operator approves the push or takes the call in the same tab; the
        # run must not touch the screen, must not send a code, and must continue
        # by itself once the signed-in page appears.
        browser = ScriptedBrowserOS([VERIFICATION_PUSH_ONLY, ACCOUNTS])
        result = asyncio.run(
            bridge.authenticate(
                browser, 7, "user-value", "password", timeout=5, poll_seconds=0
            )
        )
        self.assertIn("Sign out", result)
        self.assertEqual(browser.ops(), [])
        self.assertEqual(browser.selects(), [])

    def test_an_unrecognised_step_up_screen_also_waits(self):
        # A verification screen this bridge cannot read is handed to the operator
        # rather than aborting the download.
        renamed = page(
            SIGNON_URL,
            '- heading "Verify your identity" [level=2]\n'
            '- combobox "Contact method" [collapsed] [ref=e2]: "Push"\n'
            '- option "Push" [ref=e4]\n'
            '- button "Send" [ref=e7]',
        )
        browser = ScriptedBrowserOS([renamed, ACCOUNTS])
        result = asyncio.run(
            bridge.authenticate(
                browser, 7, "user-value", "password", timeout=5, poll_seconds=0
            )
        )
        self.assertIn("Sign out", result)
        self.assertEqual(browser.ops(), [])

    def test_a_step_up_nobody_completes_still_times_out(self):
        browser = ScriptedBrowserOS([VERIFICATION_PUSH_ONLY])
        with self.assertRaisesRegex(
            bridge.InteractiveChallengeError, "did not complete sign-in"
        ):
            asyncio.run(
                bridge.authenticate(
                    browser, 7, "user-value", "password", timeout=0.1, poll_seconds=0
                )
            )
        self.assertEqual(browser.ops(), [])

    def test_page_text_about_identity_verification_is_not_a_challenge(self):
        # Regression: ordinary page text mentioning identity verification once
        # aborted a live run that was already signed in.
        browser = self.authenticate([IDENTITY_TEXT_IN_PAGE])
        self.assertEqual(browser.ops(), [])

    def test_unknown_unchanging_screen_times_out(self):
        browser = ScriptedBrowserOS([OPAQUE])
        with self.assertRaisesRegex(
            bridge.InteractiveChallengeError, "did not complete sign-in"
        ):
            asyncio.run(
                bridge.authenticate(
                    browser, 7, "user-value", "password", timeout=0.1, poll_seconds=0
                )
            )

    @patch.object(bridge, "sms_reader_supported", return_value=False)
    def test_a_code_that_cannot_be_read_is_never_requested(self, _supported):
        # Sending an SMS this machine cannot read would only text the user, so the
        # screen is left for the operator even though SMS is offered (C4).
        browser = ScriptedBrowserOS([VERIFICATION_WITH_SMS, ACCOUNTS])
        result = asyncio.run(
            bridge.authenticate(
                browser, 7, "user-value", "password", timeout=5, poll_seconds=0
            )
        )
        self.assertIn("Sign out", result)
        self.assertEqual(browser.selects(), [])
        self.assertEqual(browser.acts("click", ref="e7"), [])

    @patch.object(bridge, "sms_reader_supported", return_value=True)
    @patch.object(bridge, "wait_for_sms_code", return_value="654321")
    def test_text_verification_selects_the_text_channel_and_fills_the_code(
        self, wait_for_code, _supported
    ):
        browser = ScriptedBrowserOS([VERIFICATION_WITH_SMS, CODE_PAGE, ACCOUNTS])
        asyncio.run(
            bridge.authenticate(
                browser, 7, "user-value", "password", timeout=5, poll_seconds=0
            )
        )
        self.assertEqual(wait_for_code.call_args.args[0], bridge.SMS_MARKERS)
        self.assertEqual(
            [selected["value"] for selected in browser.selects()],
            ["SMS 64X-XXX-X162"],
        )
        self.assertIn({"ref": "e40", "value": "654321"}, browser.fills())
        self.assertEqual(
            [arguments["ref"] for arguments in browser.acts("click")], ["e7", "e41"]
        )

    @patch.object(bridge, "sms_reader_supported", return_value=True)
    @patch.object(
        bridge,
        "wait_for_sms_code",
        side_effect=RuntimeError("macos-messages needs Full Disk Access"),
    )
    def test_an_unreadable_sms_code_exits_actionably(self, _wait_for_code, _supported):
        # A machine without macOS Messages must not crash inside the helper; the
        # run ends with what the operator has to do instead (C4, V3).
        browser = ScriptedBrowserOS([VERIFICATION_WITH_SMS, CODE_PAGE, ACCOUNTS])
        with self.assertRaisesRegex(
            bridge.InteractiveChallengeError, "could not read it"
        ):
            asyncio.run(
                bridge.authenticate(
                    browser, 7, "user-value", "password", timeout=5, poll_seconds=0
                )
            )
        self.assertEqual(
            [selected["value"] for selected in browser.selects()],
            ["SMS 64X-XXX-X162"],
        )

    def test_the_card_number_travels_as_keystrokes_not_a_fill_value(self):
        browser = self.authenticate([SIGNON, ACCOUNTS])
        self.assertEqual([field["value"] for field in browser.fills()], ["password"])
        self.assertEqual(
            [arguments["text"] for arguments in browser.acts("type")],
            ["user-value"],
        )


class TestAccountAndForm(unittest.TestCase):
    def test_account_options_match_the_requested_account(self):
        _, matches = bridge.account_choices(download_form(), "visa", None)
        self.assertEqual(matches, [CARD])
        _, debit = bridge.account_choices(download_form(), "debit", None)
        self.assertEqual(debit, [CHEQUING])

    def test_last_four_filters_the_account_options(self):
        _, matches = bridge.account_choices(download_form(), "visa", "9999")
        self.assertEqual(matches, [])

    def test_ambiguous_accounts_exit_without_selecting(self):
        form = download_form(options=(CHEQUING, OTHER_CHEQUING, CARD))
        browser = ScriptedBrowserOS([form, form])
        with self.assertRaisesRegex(RuntimeError, "SIMPLII_ACCOUNT"):
            asyncio.run(
                bridge.fill_export_form(
                    browser,
                    7,
                    form,
                    "debit",
                    date(2026, 9, 1),
                    date(2026, 10, 10),
                    None,
                )
            )
        self.assertEqual(browser.selects(), [])

    def test_last_four_disambiguates_two_matching_accounts(self):
        form = download_form(options=(CHEQUING, OTHER_CHEQUING, CARD))
        applied = applied_form(account=OTHER_CHEQUING)
        browser = ScriptedBrowserOS([applied])
        asyncio.run(
            bridge.fill_export_form(
                browser, 7, form, "debit", date(2026, 9, 1), date(2026, 10, 10), "9999"
            )
        )
        self.assertEqual(browser.selects()[0]["value"], OTHER_CHEQUING)

    def test_fills_the_account_range_dates_and_csv_format(self):
        form = download_form()
        browser = ScriptedBrowserOS([applied_form()])
        asyncio.run(
            bridge.fill_export_form(
                browser, 7, form, "debit", date(2026, 9, 1), date(2026, 10, 10), None
            )
        )
        self.assertEqual(
            [selected["value"] for selected in browser.selects()],
            [
                CHEQUING,
                "September",
                "1",
                "2026",
                "October",
                "10",
                "2026",
                "Spreadsheet (CSV)",
            ],
        )
        # The radio is covered by its wrapper, so the wrapper is clicked instead.
        self.assertEqual(
            [arguments["ref"] for arguments in browser.acts("click")], ["e49"]
        )

    def test_refuses_when_the_form_does_not_keep_the_selection(self):
        form = download_form()
        drifted = applied_form(since=("September", "2", "2026"))
        browser = ScriptedBrowserOS([drifted])
        with self.assertRaisesRegex(
            RuntimeError, "did not keep the requested From day"
        ):
            asyncio.run(
                bridge.fill_export_form(
                    browser,
                    7,
                    form,
                    "debit",
                    date(2026, 9, 1),
                    date(2026, 10, 10),
                    None,
                )
            )

    def test_triggers_the_export_with_the_download_tool(self):
        with tempfile.TemporaryDirectory() as directory:
            downloaded = Path(directory) / "SIMPLII.csv"
            downloaded.write_text("Date, Transaction Details, Funds Out, Funds In\n")
            browser = ScriptedBrowserOS(
                [download_form()], download={"path": str(downloaded)}
            )
            with patch.dict(os.environ, {"BROWSEROS_DOWNLOAD_DIR": directory}):
                result = asyncio.run(bridge.trigger_export(browser, 7, download_form()))
            self.assertEqual(result, downloaded.resolve())
        # The MCP download tool reports the artifact; the button is never clicked
        # blind, which is what makes the returned path trustworthy (V12).
        self.assertEqual(browser.calls, [("download", {"page": 7, "ref": "e180"})])

    def test_refuses_a_download_from_outside_the_browseros_directory(self):
        with tempfile.TemporaryDirectory() as directory:
            outside = Path(directory) / "elsewhere" / "SIMPLII.csv"
            with patch.dict(
                os.environ, {"BROWSEROS_DOWNLOAD_DIR": str(Path(directory) / "allowed")}
            ):
                with self.assertRaisesRegex(
                    RuntimeError, "outside its download directory"
                ):
                    asyncio.run(
                        bridge.trigger_export(
                            ScriptedBrowserOS(
                                [download_form()], download={"path": str(outside)}
                            ),
                            7,
                            download_form(),
                        )
                    )

    def test_refuses_a_non_csv_artifact(self):
        with tempfile.TemporaryDirectory() as directory:
            artifact = Path(directory) / "SIMPLII.pdf"
            with patch.dict(os.environ, {"BROWSEROS_DOWNLOAD_DIR": directory}):
                with self.assertRaisesRegex(RuntimeError, "non-CSV artifact"):
                    asyncio.run(
                        bridge.trigger_export(
                            ScriptedBrowserOS(
                                [download_form()], download={"path": str(artifact)}
                            ),
                            7,
                            download_form(),
                        )
                    )


class TestRunDownload(unittest.TestCase):
    def run_bridge(self, browser, directory, until=date(2026, 10, 10)):
        with patch.dict(
            os.environ,
            {
                "BROWSEROS_MCP_URL": "http://127.0.0.1:9239/mcp",
                "BROWSEROS_DOWNLOAD_DIR": directory,
            },
        ):
            return asyncio.run(
                bridge.run_download(
                    "visa",
                    date(2026, 9, 1),
                    until,
                    "user-value",
                    "password",
                    None,
                    session_factory=lambda endpoint: browser,
                )
            )

    def test_returns_the_exported_csv_and_closes_its_tab(self):
        with tempfile.TemporaryDirectory() as directory:
            downloaded = Path(directory) / "SIMPLII.csv"
            downloaded.write_text("Date, Transaction Details, Funds Out, Funds In\n")
            browser = ScriptedBrowserOS(
                [SIGNON, ACCOUNTS, download_form(), FILLED],
                download={"path": str(downloaded)},
            )
            result = self.run_bridge(browser, directory)
            self.assertEqual(result, downloaded.resolve())
            self.assertEqual(
                [call for call in browser.calls if call[0] == "snapshot"],
                [("snapshot", {"page": 7})] * 4,
            )
        self.assertEqual(browser.acts("click", ref="e29")[0]["kind"], "click")
        self.assertIn(
            ("tabs", {"action": "close", "page": 7}),
            browser.calls,
        )
        self.assertEqual(len(browser.acts("type")), 1)

    def test_closes_the_tab_when_sign_in_fails(self):
        with tempfile.TemporaryDirectory() as directory:
            browser = ScriptedBrowserOS([SIGNON_WITH_CAPTCHA])
            with self.assertRaises(bridge.InteractiveChallengeError):
                self.run_bridge(browser, directory)
        self.assertIn(("tabs", {"action": "close", "page": 7}), browser.calls)


if __name__ == "__main__":
    unittest.main()

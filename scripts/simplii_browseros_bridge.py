#!/usr/bin/env python3
"""Local MCP bridge for unattended Simplii account-activity downloads.

The bridge drives the BrowserOS-managed profile in a new tab: it never copies or
exports that profile (C10). Credentials are filled only after the active page
origin is confirmed to be a simplii.com host (V4), an interactive challenge ends
the run instead of being worked around (V3), and a download is accepted only when
BrowserOS reports it inside its own download directory (V12).

Discovery finding (T1): a new tab always starts signed out, even when another tab
is already signed in, so signing in is the normal path and not just the fallback.

Every Simplii screen fact lives in the VERIFIED UI FACTS block below and nowhere
else (V14).
"""

import asyncio
import logging
import re
import sys
import time
from datetime import date, datetime, timezone
from pathlib import Path
from urllib.parse import urlparse

from scripts.amex_browseros_bridge import (
    BrowserOSSession,
    BrowserOSSessionFactory,
    FastMCPBrowserOSSession,
    browseros_mcp_url,
    click,
    download_path,
    page_from_response,
    response_text,
    snapshot,
)
from scripts.browser_common import wait_for_sms_code

logger = logging.getLogger("simplii_browseros_bridge")

BASE_URL = "https://online.simplii.com/"
SIMPLII_HOST = "simplii.com"
LOGIN_TIMEOUT_SECONDS = 5 * 60
ACTION_TIMEOUT_SECONDS = 30
POLL_SECONDS = 1.0
LOGIN_REJECTED_AFTER_SECONDS = 20
MFA_CODE_INITIAL_DELAY_SECONDS = 10
MFA_CODE_TIMEOUT_SECONDS = 2 * 60
SMS_MARKERS = ("simplii",)
# Only a genuine challenge stops the run by name; an unread screen times out
# with its own actionable message instead of being guessed at.
CHALLENGE_MARKERS = ("captcha",)
NO_ACTIVE_PAGE_ERROR = "BrowserOS did not return the active page URL"

CONTROL_PATTERN = re.compile(r'^\s*- ([A-Za-z]+) "([^"]*)"[^\n]*\[ref=(e\d+)\]')
OPTION_PATTERN = re.compile(r'^\s*- option "([^"]*)"')
INTERACTIVE_PATTERN = re.compile(
    r'^\s*- (combobox|textbox|radio|button|checkbox|link|menuitem) "'
)


class InteractiveChallengeError(RuntimeError):
    """Simplii asked for something only a human can complete."""


# ---------------------------------------------------------------------------
# VERIFIED UI FACTS — observed against the live site in the T1 discovery session.
#
# The export form is one page: the accounts page's "Download Transactions"
# menuitem opens it, everything else is chosen there, and the artifact is always
# written as SIMPLII.csv in the browser's own download directory, whichever
# account was selected. Callers must therefore move the file promptly.
# ---------------------------------------------------------------------------
LOGIN_FIELDS = ("Card number", "Password")
LOGIN_BUTTON = "Sign in."
# The card field masks and reformats its input, so BrowserOS's self-verifying
# `fill` rejects it; keyboard entry after focus is the verified recipe.
KEYBOARD_ONLY_FIELDS = ("Card number",)
AUTHENTICATED_MARKER = "Sign out"
DOWNLOAD_MENU_ITEM = "Download Transactions"
ACCOUNT_FIELD = "Account:"
ACCOUNT_MARKERS = {"visa": ("visa",), "debit": ("chequing", "debit")}
RANGE_FIELD = "All within date range"
FROM_FIELDS = ("From month", "From day", "From year")
TO_FIELDS = ("To month", "To day", "To year")
MONTH_NAMES = (
    "January",
    "February",
    "March",
    "April",
    "May",
    "June",
    "July",
    "August",
    "September",
    "October",
    "November",
    "December",
)
FORMAT_FIELD = "Financial management software:"
FORMAT_CHOICE = "Spreadsheet (CSV)"
EXPORT_BUTTON = "Download Transactions"
# The exported file keeps the parser's %m/%d/%Y dates and its own header.
# Step-up verification is risk-based: it appeared on two of about a dozen
# discovery sign-ins, so it cannot be triggered on demand. The observed screen is
# heading "Verify your identity", combobox "Your contact preference" offering
# "Select", "Push to your device ********", "SMS <masked number>" and
# "Outbound call <masked number>", plus "Send" and "Cancel" buttons. The default
# contact preference has been observed as both the push and the outbound call.
# Choosing SMS sends a code; the screen that then accepts it is still
# PROVISIONAL, because this machine has no macOS Messages to read the code with.
VERIFICATION_HEADING = "Verify your identity"
VERIFICATION_CHOOSER = "Your contact preference"
VERIFICATION_SEND = "Send"
SMS_CHOICES = ("sms",)
PUSH_CHOICES = ("push to your device",)
VERIFICATION_CODE_FIELD = "Verification code"
VERIFICATION_BUTTON = "Continue"
# ---------------------------------------------------------------------------


def page_url(current: str) -> str:
    match = re.match(
        r"^\[UNTRUSTED_PAGE_CONTENT\b[^\]]*\borigin=(https://[^\s\]]+)", current
    )
    if match is None:
        raise RuntimeError(NO_ACTIVE_PAGE_ERROR)
    return match.group(1)


def require_simplii_url(current: str) -> str:
    url = page_url(current)
    hostname = urlparse(url).hostname
    if hostname is None or not (
        hostname == SIMPLII_HOST or hostname.endswith(f".{SIMPLII_HOST}")
    ):
        raise RuntimeError("Refusing to fill credentials outside simplii.com")
    return url


def controls(
    current: str, roles: tuple[str, ...] | None = None
) -> list[tuple[str, str, str]]:
    """Every named, referenced control in a snapshot as (role, name, ref)."""
    found = []
    for line in current.splitlines():
        match = CONTROL_PATTERN.match(line)
        if match and (roles is None or match.group(1) in roles):
            found.append((match.group(1), match.group(2), match.group(3)))
    return found


def has_heading(current: str, name: str) -> bool:
    """Whether a snapshot carries this heading (headings need not be refs)."""
    return re.search(rf'^\s*- heading "{re.escape(name)}"', current, re.M) is not None


def find_control(
    current: str, names: tuple[str, ...], roles: tuple[str, ...]
) -> tuple[str, str] | None:
    for _, name, ref in controls(current, roles):
        if any(candidate.lower() in name.lower() for candidate in names):
            return name, ref
    return None


def require_control(
    current: str, names: tuple[str, ...], roles: tuple[str, ...], error: str
) -> str:
    found = find_control(current, names, roles)
    if found is None:
        raise RuntimeError(error)
    return found[1]


def displayed_value(current: str, ref: str) -> str | None:
    match = re.search(rf'\[ref={re.escape(ref)}\]:\s*"([^"]*)"', current)
    return match.group(1) if match else None


def state_of(
    current: str, names: tuple[str, ...], roles: tuple[str, ...]
) -> str | None:
    """The value a control reports now, looked up afresh so refs may shift."""
    found = find_control(current, names, roles)
    return None if found is None else displayed_value(current, found[1])


def options_of(current: str, combobox_ref: str) -> list[str]:
    """Option labels belonging to one combobox, read from its own block."""
    lines = current.splitlines()
    start = None
    for index, line in enumerate(lines):
        if line.strip().startswith("- combobox") and f"[ref={combobox_ref}]" in line:
            start = index + 1
            break
    if start is None:
        return []
    labels = []
    for line in lines[start:]:
        if match := OPTION_PATTERN.match(line):
            labels.append(match.group(1))
            continue
        if INTERACTIVE_PATTERN.match(line):
            break
    return labels


def account_digits(label: str) -> str:
    match = re.search(r"\(([^)]*)\)", label)
    if match is not None:
        return re.sub(r"\D", "", match.group(1))
    return re.sub(r"\D", "", label)


def account_choices(
    current: str, account: str, last_four: str | None
) -> tuple[str, list[str]]:
    ref = require_control(
        current,
        (ACCOUNT_FIELD,),
        ("combobox",),
        "Simplii page changed: the download account list is unavailable",
    )
    options = options_of(current, ref)
    matches = [
        label
        for label in options
        if any(marker in label.lower() for marker in ACCOUNT_MARKERS[account])
    ]
    if last_four:
        matches = [
            label for label in matches if account_digits(label).endswith(last_four)
        ]
    return ref, matches


def credentials_fields(current: str) -> list[tuple[str, str]] | None:
    """(label, ref) for the sign-on fields, or None when this is not sign-on."""
    fields = []
    for label in LOGIN_FIELDS:
        found = find_control(current, (label,), ("textbox",))
        if found is None:
            return None
        fields.append((label, found[1]))
    return fields


def is_authenticated_page(current: str) -> bool:
    if credentials_fields(current) is not None:
        return False
    return AUTHENTICATED_MARKER.lower() in current.lower()


def require_no_interactive_challenge(current: str) -> None:
    normalized = current.lower()
    if any(marker in normalized for marker in CHALLENGE_MARKERS):
        raise InteractiveChallengeError(
            "Simplii requires an interactive security challenge"
        )


async def act(
    session: BrowserOSSession, page: int, kind: str, **arguments: object
) -> str:
    return response_text(
        await session.call(
            "act", {"page": page, "kind": kind, "diff": "none", **arguments}
        )
    )


async def wait_for(
    session: BrowserOSSession,
    page: int,
    predicate,
    description: str,
    *,
    timeout: float = ACTION_TIMEOUT_SECONDS,
    poll_seconds: float = 0.5,
) -> str:
    deadline = time.monotonic() + timeout
    current = ""
    while time.monotonic() < deadline:
        current = await snapshot(session, page)
        if predicate(current):
            return current
        await asyncio.sleep(poll_seconds)
    raise RuntimeError(f"Timed out waiting for Simplii {description}")


async def fill_login(
    session: BrowserOSSession,
    page: int,
    current: str,
    fields: list[tuple[str, str]],
    user: str,
    password: str,
) -> None:
    require_simplii_url(current)
    card_label, card_ref = fields[0]
    _, password_ref = fields[1]
    if card_label in KEYBOARD_ONLY_FIELDS:
        if displayed_value(current, card_ref):
            await act(session, page, "fill", fields=[{"ref": card_ref, "value": ""}])
        await act(session, page, "focus", ref=card_ref)
        await act(session, page, "type", text=user)
    else:
        await act(session, page, "fill", fields=[{"ref": card_ref, "value": user}])
    await act(session, page, "fill", fields=[{"ref": password_ref, "value": password}])
    button = require_control(
        current,
        (LOGIN_BUTTON,),
        ("button",),
        "Simplii page changed: the sign-on button is unavailable",
    )
    await click(session, page, button)


def sms_reader_supported() -> bool:
    """T2's code reader reads macOS Messages, so only macOS can consume an SMS."""
    return sys.platform == "darwin"


def text_message_option(current: str) -> tuple[str, str] | None:
    """(chooser ref, option label) when Simplii offers a text message at all."""
    chooser = find_control(current, (VERIFICATION_CHOOSER,), ("combobox",))
    if chooser is None:
        return None
    for label in options_of(current, chooser[1]):
        if any(marker in label.lower() for marker in SMS_CHOICES):
            return chooser[1], label
    return None


def offered_channels(current: str) -> list[str]:
    """The step-up methods Simplii is offering, in words, for the operator."""
    chooser = find_control(current, (VERIFICATION_CHOOSER,), ("combobox",))
    if chooser is None:
        return []
    channels = []
    for label in options_of(current, chooser[1]):
        lowered = label.lower()
        if lowered.startswith("select"):
            continue
        if any(marker in lowered for marker in PUSH_CHOICES):
            channels.append("a push to your device")
        elif any(marker in lowered for marker in SMS_CHOICES):
            channels.append("a text message")
        elif "call" in lowered:
            channels.append("an outbound call")
        else:
            channels.append("a method this bridge does not recognise")
    return channels


async def complete_text_message_verification(
    session: BrowserOSSession,
    page: int,
    current: str,
    choice: tuple[str, str],
) -> None:
    """Answer the step-up screen with a text message this machine can read (C4)."""
    combobox_ref, label = choice
    await select(session, page, combobox_ref, label)
    send_ref = require_control(
        current,
        (VERIFICATION_SEND,),
        ("button",),
        "Simplii page changed: the verification Send button is unavailable",
    )
    await click(session, page, send_ref)
    requested_at = datetime.now(timezone.utc).replace(tzinfo=None)
    code_page = await wait_for(
        session,
        page,
        lambda snapshot: VERIFICATION_CODE_FIELD.lower() in snapshot.lower(),
        repr(VERIFICATION_CODE_FIELD),
    )
    try:
        code = await asyncio.to_thread(
            wait_for_sms_code,
            SMS_MARKERS,
            requested_at,
            source="Simplii",
            initial_delay_seconds=MFA_CODE_INITIAL_DELAY_SECONDS,
            timeout_seconds=MFA_CODE_TIMEOUT_SECONDS,
        )
    except (RuntimeError, OSError) as exc:
        raise InteractiveChallengeError(
            "Simplii sent a verification code by SMS but this machine could not "
            f"read it ({exc}); read the code yourself and run the download again"
        ) from exc
    field_ref = require_control(
        code_page,
        (VERIFICATION_CODE_FIELD,),
        ("textbox",),
        "Simplii page changed: the verification code field is unavailable",
    )
    await act(session, page, "fill", fields=[{"ref": field_ref, "value": code}])
    if (
        submit := find_control(code_page, (VERIFICATION_BUTTON,), ("button",))
    ) is not None:
        await click(session, page, submit[1])


async def authenticate(
    session: BrowserOSSession,
    page: int,
    user: str,
    password: str,
    *,
    timeout: float = LOGIN_TIMEOUT_SECONDS,
    poll_seconds: float = POLL_SECONDS,
) -> str:
    """Sign in when needed and return the authenticated page snapshot."""
    deadline = time.monotonic() + timeout
    credentials_sent_at: float | None = None
    verification_seen = False
    while time.monotonic() < deadline:
        current = await snapshot(session, page)
        try:
            require_simplii_url(current)
        except RuntimeError as exc:
            if str(exc) != NO_ACTIVE_PAGE_ERROR:
                raise
            await asyncio.sleep(poll_seconds)
            continue
        require_no_interactive_challenge(current)
        if is_authenticated_page(current):
            return current
        # Step-up verification is handled once; after that this loop only waits,
        # so the operator's own approval can finish it and a code is never
        # re-requested in a loop (V3).
        if not verification_seen and has_heading(current, VERIFICATION_HEADING):
            verification_seen = True
            choice = text_message_option(current) if sms_reader_supported() else None
            if choice is not None:
                await complete_text_message_verification(session, page, current, choice)
            else:
                # Simplii is asking for a push approval, a phone call, or a text
                # this machine cannot read. Leave the screen to the operator and
                # keep watching for the signed-in page (C4, I5).
                wait_minutes = max(1, round((deadline - time.monotonic()) / 60))
                logger.warning(
                    "Simplii is asking for step-up verification (%s). Complete it "
                    "in the open BrowserOS tab within the next %s; the download "
                    "continues by itself once you are signed in.",
                    ", ".join(offered_channels(current))
                    or "no method this run can read",
                    "minute" if wait_minutes == 1 else f"{wait_minutes} minutes",
                )
            continue
        fields = credentials_fields(current)
        if fields is not None and credentials_sent_at is None:
            await fill_login(session, page, current, fields, user, password)
            credentials_sent_at = time.monotonic()
            continue
        if (
            fields is not None
            and credentials_sent_at is not None
            and not verification_seen
            and time.monotonic() - credentials_sent_at > LOGIN_REJECTED_AFTER_SECONDS
        ):
            raise RuntimeError("Simplii rejected the login credentials")
        await asyncio.sleep(poll_seconds)
    raise InteractiveChallengeError("Simplii did not complete sign-in unattended")


async def open_download_page(session: BrowserOSSession, page: int, current: str) -> str:
    """Open the export form from the accounts page's Download Transactions menu."""
    menu_ref = require_control(
        current,
        (DOWNLOAD_MENU_ITEM,),
        ("menuitem", "link"),
        "Simplii page changed: the Download Transactions menu is unavailable",
    )
    await click(session, page, menu_ref)
    return await wait_for(
        session,
        page,
        lambda snapshot: (
            find_control(snapshot, (ACCOUNT_FIELD,), ("combobox",)) is not None
        ),
        "the download account list",
    )


async def select(session: BrowserOSSession, page: int, ref: str, value: str) -> None:
    await act(session, page, "select", ref=ref, value=value)


async def choose_account(
    session: BrowserOSSession,
    page: int,
    current: str,
    account: str,
    last_four: str | None,
) -> str:
    ref, matches = account_choices(current, account, last_four)
    if len(matches) != 1:
        raise RuntimeError(
            f"Simplii lists {len(matches)} {account} accounts to download; "
            "set SIMPLII_ACCOUNT to the account's last four digits"
        )
    await select(session, page, ref, matches[0])
    return matches[0]


async def apply_date(
    session: BrowserOSSession,
    page: int,
    current: str,
    fields: tuple[str, ...],
    value: date,
) -> list[tuple[tuple[str, ...], str]]:
    choices = (
        (fields[0], MONTH_NAMES[value.month - 1]),
        (fields[1], str(value.day)),
        (fields[2], str(value.year)),
    )
    expected = []
    for name, chosen in choices:
        ref = require_control(
            current,
            (name,),
            ("combobox",),
            f"Simplii page changed: the {name} control is unavailable",
        )
        await select(session, page, ref, chosen)
        expected.append(((name,), chosen))
    return expected


async def choose_range(session: BrowserOSSession, page: int, current: str) -> None:
    """Pick the explicit date range; the radio itself is covered by its wrapper."""
    wrapper = find_control(current, (RANGE_FIELD,), ("generic",))
    radio = find_control(current, (RANGE_FIELD,), ("radio",))
    if wrapper is None and radio is None:
        raise RuntimeError(
            "Simplii page changed: the export date-range choice is unavailable"
        )
    await click(session, page, (wrapper or radio)[1])


async def choose_format(session: BrowserOSSession, page: int, current: str) -> None:
    ref = require_control(
        current,
        (FORMAT_FIELD,),
        ("combobox",),
        "Simplii page changed: the export format list is unavailable",
    )
    await select(session, page, ref, FORMAT_CHOICE)


async def fill_export_form(
    session: BrowserOSSession,
    page: int,
    current: str,
    account: str,
    since: date,
    until: date,
    last_four: str | None,
) -> str:
    account_label = await choose_account(session, page, current, account, last_four)
    await choose_range(session, page, current)
    expected = [((ACCOUNT_FIELD,), account_label)]
    expected += await apply_date(session, page, current, FROM_FIELDS, since)
    expected += await apply_date(session, page, current, TO_FIELDS, until)
    await choose_format(session, page, current)
    expected.append(((FORMAT_FIELD,), FORMAT_CHOICE))
    filled = await snapshot(session, page)
    for names, wanted in expected:
        if (state_of(filled, names, ("combobox",)) or "").strip() != wanted.strip():
            raise RuntimeError(
                "Simplii page changed: the export form did not keep the "
                f"requested {names[0]} selection"
            )
    return filled


async def trigger_export(session: BrowserOSSession, page: int, current: str) -> Path:
    button_ref = require_control(
        current,
        (EXPORT_BUTTON,),
        ("button",),
        "Simplii page changed: the export button is unavailable",
    )
    downloaded = download_path(
        await session.call("download", {"page": page, "ref": button_ref})
    )
    if downloaded.suffix.lower() != ".csv":
        raise RuntimeError(f"Simplii exported a non-CSV artifact: {downloaded.name}")
    return downloaded


async def run_download(
    account: str,
    since: date,
    until: date,
    user: str,
    password: str,
    last_four: str | None,
    *,
    session_factory: BrowserOSSessionFactory = FastMCPBrowserOSSession,
) -> Path:
    async with session_factory(browseros_mcp_url()) as session:
        page = page_from_response(
            await session.call("tabs", {"action": "new", "url": BASE_URL})
        )
        try:
            authenticated = await authenticate(session, page, user, password)
            form = await open_download_page(session, page, authenticated)
            filled = await fill_export_form(
                session, page, form, account, since, until, last_four
            )
            downloaded = await trigger_export(session, page, filled)
            logger.info("Simplii exported %s activity to %s", account, downloaded.name)
            return downloaded
        finally:
            try:
                await session.call("tabs", {"action": "close", "page": page})
            except RuntimeError:
                pass

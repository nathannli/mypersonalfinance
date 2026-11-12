#!/usr/bin/env python3
"""Local MCP bridge for Rogers Bank account-activity downloads.

It drives the BrowserOS-managed profile in a new tab, fills credentials only
after the page origin is confirmed to be a rogersbank.com host, and ends the
run on a CAPTCHA instead of working around it. Rogers' identity verification
offers several delivery options; the bridge selects the one ending in 5162,
reads the SMS code from this machine's Messages database through the shared
wait_for_sms_code helper, and fills it. It then exports the card activity as
CSV, one statement month at a time. Every download is saved by the browser as
transactions.csv, so each one is moved at once into a private temporary folder
and the paths are returned to the downloader.

Every Rogers screen fact lives in the VERIFIED UI FACTS block below and
nowhere else.
"""

import asyncio
import logging
import re
import shutil
import tempfile
import time
from collections.abc import Callable
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
    snapshot,
)
from scripts.browser_common import move_download, wait_for_sms_code
from scripts.simplii_browseros_bridge import (
    NO_ACTIVE_PAGE_ERROR,
    InteractiveChallengeError,
    act,
    controls,
    displayed_value,
    find_control,
    page_url,
)

logger = logging.getLogger("rogers_browseros_bridge")

LOGIN_URL = "https://selfserve.rogersbank.com/sign-in?locale=en"
TRANSACTIONS_URL = "https://selfserve.rogersbank.com/transactions"
ROGERS_HOST = "rogersbank.com"
LOGIN_TIMEOUT_SECONDS = 5 * 60
LOGIN_REJECTED_AFTER_SECONDS = 20
POLL_SECONDS = 1.0
# The sign-in page always embeds a reCAPTCHA iframe, so the challenge is only
# real when its interactive text shows; the word "captcha" alone is not it.
CHALLENGE_MARKERS = ("not a robot", "verify you are human", "select all images")
SMS_MARKERS = ("rogers bank",)
SMS_CODE_PATTERN = re.compile(r"\b(\d{8})\b")
MFA_CODE_INITIAL_DELAY_SECONDS = 2
MFA_CODE_TIMEOUT_SECONDS = 3 * 60

# ---------------------------------------------------------------------------
# VERIFIED UI FACTS, observed against the live site on 2026-11-08.
#
# www.rogersbank.com/en/login is a dead error page; sign-in lives at
# selfserve.rogersbank.com/sign-in. Its form has an "Enter username" textbox,
# an "Enter password" textbox and a "Sign in" button that enables once both
# are filled. A reCAPTCHA iframe is present but showed no challenge. Accepted
# credentials open an "Identity verification" dialog whose radiogroup offers a
# masked email, a number labelled Mobile and a number labelled Home; selecting
# a radio enables "Send code". After that, a "Verification Code" textbox and a
# "Continue" button appear, and the page states that text codes come from
# 74979. Signed in, the app sits at selfserve.rogersbank.com/home with a
# "Sign Out" button and one Rogers Red World Elite Mastercard.
#
# The transactions page (selfserve.rogersbank.com/transactions) has a
# "Download transactions" control opening a dialog with a "Select Statement
# Period" combobox ("Current transactions", then one "Mon YYYY" option per
# statement month, about 13 offered) and CSV preselected as the type. The
# periods are statement cycles, not calendar months. The browser saves every
# export as transactions.csv.
# ---------------------------------------------------------------------------
USER_FIELD = "Enter username"
PASSWORD_FIELD = "Enter password"
SIGN_IN_BUTTON = "Sign in"
AUTHENTICATED_MARKER = "Sign Out"
SIGN_OUT_BUTTON = "Sign Out"
VERIFICATION_HEADING = "Identity verification"
VERIFICATION_OPTION_SUFFIX = "5162"
SEND_CODE_BUTTON = "Send code"
CODE_FIELD = "Verification Code"
CONTINUE_BUTTON = "Continue"
DOWNLOAD_TRANSACTIONS = "Download transactions"
PERIOD_FIELD = "Select Statement Period"
CURRENT_PERIOD = "Current transactions"
PERIOD_PATTERN = re.compile(r"^([A-Z][a-z]{2}) (\d{4})$")
DOWNLOAD_BUTTON = "Download"
MAX_HISTORY_DAYS = 400
ACTION_TIMEOUT_SECONDS = 30
DOWNLOAD_TIMEOUT_SECONDS = 180


def require_rogers_url(current: str) -> str:
    url = page_url(current)
    hostname = urlparse(url).hostname
    if hostname is None or not (
        hostname == ROGERS_HOST or hostname.endswith(f".{ROGERS_HOST}")
    ):
        raise RuntimeError("Refusing to fill credentials outside rogersbank.com")
    return url


def require_no_interactive_challenge(current: str) -> None:
    normalized = current.lower()
    if any(marker in normalized for marker in CHALLENGE_MARKERS):
        raise InteractiveChallengeError(
            "Rogers requires an interactive security challenge"
        )


def has_heading(current: str, name: str) -> bool:
    return re.search(rf'^\s*- heading "{re.escape(name)}"', current, re.M) is not None


def sign_on_form(current: str) -> tuple[str, str] | None:
    """(user ref, password ref) when this is the sign-on form."""
    user = find_control(current, (USER_FIELD,), ("textbox",))
    password = find_control(current, (PASSWORD_FIELD,), ("textbox",))
    if user is None or password is None:
        return None
    return user[1], password[1]


def is_authenticated_page(current: str) -> bool:
    return sign_on_form(current) is None and AUTHENTICATED_MARKER in current


async def fill_login(
    session: BrowserOSSession, page: int, current: str, user: str, password: str
) -> None:
    require_rogers_url(current)
    form = sign_on_form(current)
    if form is None:
        raise RuntimeError("Rogers page changed: the sign-on form is unavailable")
    user_ref, password_ref = form
    button = find_control(current, (SIGN_IN_BUTTON,), ("button",))
    if button is None:
        raise RuntimeError("Rogers page changed: the sign-in button is unavailable")
    await act(
        session,
        page,
        "fill",
        fields=[{"ref": user_ref, "value": user}],
        sensitive=True,
    )
    await act(
        session,
        page,
        "fill",
        fields=[{"ref": password_ref, "value": password}],
        sensitive=True,
    )
    current = await snapshot(session, page)
    button = find_control(current, (SIGN_IN_BUTTON,), ("button",))
    if button is None:
        raise RuntimeError("Rogers page changed: the sign-in button is unavailable")
    await click(session, page, button[1])


def verification_option(current: str) -> str | None:
    """Ref of the identity-verification radio ending in the approved suffix."""
    for _, name, ref in controls(current, ("radio",)):
        if name.strip().endswith(VERIFICATION_OPTION_SUFFIX):
            return ref
    return None


async def complete_verification(
    session: BrowserOSSession, page: int, current: str
) -> None:
    """Answer the identity check with the 5162 option and the Messages code."""
    ref = verification_option(current)
    if ref is None:
        raise InteractiveChallengeError(
            "Rogers did not offer the approved verification option ending "
            f"{VERIFICATION_OPTION_SUFFIX}"
        )
    await click(session, page, ref)
    current = await snapshot(session, page)
    send = find_control(current, (SEND_CODE_BUTTON,), ("button",))
    if send is None:
        raise RuntimeError("Rogers page changed: the Send code button is unavailable")
    requested_at = datetime.now(timezone.utc).replace(tzinfo=None)
    await click(session, page, send[1])
    code_page = await wait_for(
        session,
        page,
        lambda c: find_control(c, (CODE_FIELD,), ("textbox",)) is not None,
        "the verification code field",
    )
    try:
        code = await asyncio.to_thread(
            wait_for_sms_code,
            SMS_MARKERS,
            requested_at,
            source="Rogers Bank",
            initial_delay_seconds=MFA_CODE_INITIAL_DELAY_SECONDS,
            timeout_seconds=MFA_CODE_TIMEOUT_SECONDS,
            pattern=SMS_CODE_PATTERN,
        )
    except (RuntimeError, OSError) as exc:
        raise InteractiveChallengeError(
            "Rogers sent a verification code by SMS but this machine could not "
            f"read it ({exc}); read the code yourself and run the download again"
        ) from exc
    field = find_control(code_page, (CODE_FIELD,), ("textbox",))
    if field is None:
        raise RuntimeError("Rogers page changed: the code field is unavailable")
    await act(session, page, "fill", fields=[{"ref": field[1], "value": code}])
    current = await snapshot(session, page)
    submit = find_control(current, (CONTINUE_BUTTON,), ("button",))
    if submit is None:
        raise RuntimeError("Rogers page changed: the Continue button is unavailable")
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
            require_rogers_url(current)
        except RuntimeError as exc:
            if str(exc) != NO_ACTIVE_PAGE_ERROR:
                raise
            await asyncio.sleep(poll_seconds)
            continue
        require_no_interactive_challenge(current)
        if is_authenticated_page(current):
            return current
        if not verification_seen and has_heading(current, VERIFICATION_HEADING):
            verification_seen = True
            await complete_verification(session, page, current)
            continue
        if sign_on_form(current) is not None:
            if credentials_sent_at is None:
                await fill_login(session, page, current, user, password)
                credentials_sent_at = time.monotonic()
                continue
            if (
                not verification_seen
                and time.monotonic() - credentials_sent_at
                > LOGIN_REJECTED_AFTER_SECONDS
            ):
                raise RuntimeError("Rogers rejected the login credentials")
        await asyncio.sleep(poll_seconds)
    raise InteractiveChallengeError(
        "Rogers did not complete sign-in before the timeout"
    )


async def wait_for(
    session: BrowserOSSession,
    page: int,
    predicate: Callable[[str], bool],
    description: str,
    *,
    timeout: float = ACTION_TIMEOUT_SECONDS,
    poll_seconds: float = 0.5,
) -> str:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        current = await snapshot(session, page)
        require_no_interactive_challenge(current)
        if predicate(current):
            return current
        await asyncio.sleep(poll_seconds)
    raise RuntimeError(f"Timed out waiting for Rogers {description}")


def control_named(current: str, name: str, roles: tuple[str, ...]) -> str | None:
    """Ref of the control whose whole name is `name`, ignoring case and padding."""
    for _, found, ref in controls(current, roles):
        if found.strip().lower() == name.lower():
            return ref
    return None


def require_named(current: str, name: str, roles: tuple[str, ...], error: str) -> str:
    ref = control_named(current, name, roles)
    if ref is None:
        raise RuntimeError(error)
    return ref


def require_history_window(since: date, today: date | None = None) -> None:
    today = today or date.today()
    if (today - since).days > MAX_HISTORY_DAYS:
        raise RuntimeError(
            "Rogers offers about 13 statement months; --since is older than that"
        )


def period_labels(current: str) -> list[str]:
    """Statement period labels offered in the download dialog.

    "Current transactions" is offered but its Download produces no file
    (verified 2026-11-08: the dialog closes and nothing is saved), so it is
    not a usable period; those rows arrive with the next statement month.
    """
    return [
        name
        for _, name, _ in controls(current, ("option",))
        if PERIOD_PATTERN.match(name)
    ]


def period_bounds(label: str, today: date | None = None) -> tuple[date, date]:
    """Rough (first, last) days a period label can contain.

    Statement cycles straddle calendar months, so the month named by the
    label is widened by one month on the early side; the caller date-filters
    the merged rows anyway.
    """
    today = today or date.today()
    if label == CURRENT_PERIOD:
        first = today.replace(day=1)
        early = (
            date(first.year - 1, 12, 1)
            if first.month == 1
            else date(first.year, first.month - 1, 1)
        )
        return early, today
    match = PERIOD_PATTERN.match(label)
    if match is None:
        raise ValueError(f"unknown Rogers statement period: {label}")
    month = datetime.strptime(match.group(1), "%b").month
    year = int(match.group(2))
    early = date(year - 1, 12, 1) if month == 1 else date(year, month - 1, 1)
    next_month = date(year + 1, 1, 1) if month == 12 else date(year, month + 1, 1)
    late = date.fromordinal(next_month.toordinal() - 1)
    return early, late


def periods_overlapping(labels: list[str], since: date, until: date) -> list[str]:
    """The period labels touching since..until, oldest first."""

    def key(label: str) -> date:
        # Current transactions is always the newest period, whatever its
        # widened bounds say.
        return date.max if label == CURRENT_PERIOD else period_bounds(label)[0]

    wanted = [
        label
        for label in labels
        if period_bounds(label)[0] <= until and period_bounds(label)[1] >= since
    ]
    return sorted(set(wanted), key=key)


def download_transactions_ref(current: str) -> str | None:
    """Ref of the Download transactions control.

    In the snapshot it is a nameless generic whose child paragraph carries
    the text, so the ref sits on the line above the paragraph.
    """
    ref: str | None = None
    for line in current.splitlines():
        match = re.match(r"\s*- generic \[ref=(e\d+)\]", line)
        if match:
            ref = match.group(1)
        if f'"{DOWNLOAD_TRANSACTIONS}"' in line and ref is not None:
            return ref
    return None


async def open_download_dialog(
    session: BrowserOSSession, page: int, current: str
) -> str:
    if control_named(current, PERIOD_FIELD, ("combobox",)) is not None:
        return current
    ref = download_transactions_ref(current)
    if ref is None:
        raise RuntimeError("Rogers page changed: Download transactions is unavailable")
    await click(session, page, ref)
    return await wait_for(
        session,
        page,
        lambda c: control_named(c, PERIOD_FIELD, ("combobox",)) is not None,
        "the download dialog",
    )


def period_selected(current: str, label: str) -> bool:
    ref = control_named(current, PERIOD_FIELD, ("combobox",))
    return ref is not None and displayed_value(current, ref) == label


async def export_period(
    session: BrowserOSSession, page: int, current: str, workdir: Path, label: str
) -> Path:
    ref = require_named(
        current,
        DOWNLOAD_BUTTON,
        ("button",),
        "Rogers page changed: the dialog Download button is unavailable",
    )
    downloaded = download_path(
        await session.call(
            "download",
            {"page": page, "ref": ref},
            timeout=DOWNLOAD_TIMEOUT_SECONDS,
        )
    )
    if downloaded.suffix.lower() != ".csv":
        raise RuntimeError(f"Rogers exported a non-CSV artifact: {downloaded.name}")
    safe = re.sub(r"[^A-Za-z0-9]+", "-", label).strip("-").lower()
    return move_download(downloaded, workdir, f"{safe}.csv")


async def export_activity(
    session: BrowserOSSession,
    page: int,
    since: date,
    until: date,
    workdir: Path,
) -> list[Path]:
    await session.call(
        "navigate", {"action": "url", "page": page, "url": TRANSACTIONS_URL}
    )
    page_snapshot = await wait_for(
        session,
        page,
        lambda c: download_transactions_ref(c) is not None,
        "the transactions page",
    )
    dialog = await open_download_dialog(session, page, page_snapshot)
    labels = period_labels(dialog)
    wanted = periods_overlapping(labels, since, until)
    if not wanted:
        raise RuntimeError(f"Rogers lists no statement period from {since} to {until}")
    files: list[Path] = []
    for label in wanted:
        combobox = require_named(
            dialog,
            PERIOD_FIELD,
            ("combobox",),
            "Rogers page changed: the statement period list is unavailable",
        )
        await act(session, page, "select", ref=combobox, value=label)
        current = await wait_for(
            session,
            page,
            lambda c, label=label: period_selected(c, label),
            f"the {label} period to be selected",
        )
        files.append(await export_period(session, page, current, workdir, label))
        dialog = await open_download_dialog(
            session, page, await snapshot(session, page)
        )
    return files


async def sign_out(session: BrowserOSSession, page: int) -> None:
    """Best effort: leave no signed-in parents session behind."""
    try:
        current = await snapshot(session, page)
        ref = control_named(current, SIGN_OUT_BUTTON, ("button",))
        if ref is not None:
            await click(session, page, ref)
            await asyncio.sleep(2)
    except RuntimeError:
        pass


async def run_download(
    since: date,
    until: date,
    user: str,
    password: str,
    *,
    session_factory: BrowserOSSessionFactory = FastMCPBrowserOSSession,
) -> list[Path]:
    """Sign in, export the card activity, and return the raw CSV paths.

    The files sit in a private temporary folder the caller must remove. On any
    failure the folder is removed here.
    """
    require_history_window(since)
    workdir = Path(tempfile.mkdtemp(prefix="rogers-export-"))
    try:
        async with session_factory(browseros_mcp_url()) as session:
            page = page_from_response(
                await session.call("tabs", {"action": "new", "url": LOGIN_URL})
            )
            try:
                await authenticate(session, page, user, password)
                files = await export_activity(
                    session, page, since, min(until, date.today()), workdir
                )
                logger.info("Rogers exported activity (%s file(s))", len(files))
                return files
            finally:
                await sign_out(session, page)
                try:
                    await session.call("tabs", {"action": "close", "page": page})
                except RuntimeError:
                    pass
    except BaseException:
        shutil.rmtree(workdir, ignore_errors=True)
        raise

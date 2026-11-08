#!/usr/bin/env python3
"""Local MCP bridge for TD EasyWeb account-activity downloads.

It drives the BrowserOS-managed profile in a new tab, fills credentials only
after the page origin is confirmed to be a td.com host, and ends the run on a
CAPTCHA instead of working around it. TD's two-step verification is a push
approval in the TD app, which a person must give: the bridge posts one notice to
the Discord alert bot and then waits for the signed-in page. It then exports the
chequing or Visa activity as CSV. Every download is saved by the browser as
accountactivity.csv, so each one is moved at once into a private temporary
folder and the paths are returned to the downloader.

Every TD screen fact lives in the VERIFIED UI FACTS block below and nowhere else.
"""

import asyncio
import json
import logging
import os
import re
import shutil
import tempfile
import time
import urllib.request
from collections.abc import Callable
from datetime import date, datetime, timedelta
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
from scripts.browser_common import move_download
from scripts.simplii_browseros_bridge import (
    NO_ACTIVE_PAGE_ERROR,
    InteractiveChallengeError,
    act,
    controls,
    displayed_value,
    find_control,
    page_url,
)

logger = logging.getLogger("td_browseros_bridge")

LOGIN_URL = "https://authentication.td.com/uap-ui/?consumer=easyweb&locale=en_CA"
TD_HOST = "td.com"
LOGIN_TIMEOUT_SECONDS = 5 * 60
LOGIN_REJECTED_AFTER_SECONDS = 20
APPROVAL_NOTICE_AFTER_SECONDS = 8
POLL_SECONDS = 1.0
ALERT_TIMEOUT_SECONDS = 10
CHALLENGE_MARKERS = ("captcha",)
APPROVAL_NOTICE = (
    "TD sign-in is waiting: open the TD app and approve the login request."
)

# ---------------------------------------------------------------------------
# VERIFIED UI FACTS, observed against the live site on 2026-11-06.
#
# The sign-on page is https://authentication.td.com/uap-ui/. Its form has a
# "Username or Access Card" field and a "Password" textbox. Once TD remembers the
# card, the first field is a collapsed combobox that shows the masked card instead
# of a textbox. A nav button also named "Login" sits above the form, so the form
# is submitted with Enter in the password field, not by clicking a "Login" button.
# A "Privacy" alert dialog with a "Close" button can cover the first load.
# Signed in, the page moves to easyweb.td.com and carries a "Logout" link.
#
# NOT YET OBSERVED: the wording of the push-approval screen. The bridge does not
# depend on it: a page that is neither the sign-on form nor signed in, for longer
# than APPROVAL_NOTICE_AFTER_SECONDS, is treated as waiting for approval.
# ---------------------------------------------------------------------------
USER_FIELD = "Username or Access Card"
PASSWORD_FIELD = "Password"
AUTHENTICATED_MARKER = 'link "Logout"'
PRIVACY_DIALOG = 'alertdialog "Privacy"'
PRIVACY_CLOSE = "Close"

# Account list: each account is a link. The chequing link reads "Joint C$ Cheq
# A/c" and the card link "TD CASH BACK VISA INFINITE* CARD".
ACCOUNT_MARKERS = {"debit": ("cheq",), "visa": ("visa",)}
LOGOUT_LINK = "Logout"
# Chequing activity page: a "Quick filters" combobox, a "Filter" button opening a
# form with "From" and "To" textboxes (typed as MM/DD/YYYY, shown as "Oct 8,
# 2026") and an "Apply filters" button. After applying, Quick filters reads
# "Custom" and the table's date cells fall inside the range. The page's
# "Download" button opens the dialog; the range exported is the filtered range.
# TD keeps about 18 months of history here.
QUICK_FILTERS = "Quick filters"
FILTER_BUTTON = "Filter"
FROM_FIELD = "From"
TO_FIELD = "To"
APPLY_FILTERS = "Apply filters"
CUSTOM_RANGE = "Custom"
# The transaction table's date cells read like "Oct 8, 2026". The range text above
# the table is not in the accessibility snapshot, so the cells are what is checked.
TABLE_DATE_PATTERN = re.compile(r'^\s*- cell "([A-Z][a-z]{2} \d{1,2}, \d{4})"', re.M)
TABLE_DATE_FORMAT = "%b %d, %Y"
PAGE_DOWNLOAD = "Download"
MAX_HISTORY_DAYS = 548
# Visa activity is a separate app. It renders blank for several seconds, then
# shows a lowercase "download" button. Its dialog has a "Select Period" combobox
# listing one statement period per option and downloads one period at a time.
VISA_DOWNLOAD = "download"
PERIOD_FIELD = "Select Period"
PERIOD_PATTERN = re.compile(
    r"^([A-Z][a-z]{2} \d{2}, \d{4}) (?:-|to) (?:([A-Z][a-z]{2} \d{2}, \d{4})|[Tt]oday)"
)
PERIOD_DATE_FORMAT = "%b %d, %Y"
# Both download dialogs share this title, a "Select Download Format" combobox
# whose CSV choice reads "Spreadsheet (.CSV)", and a "Download" button. The
# dialog stays open after a download. Its controls come after the dialog line in
# a snapshot, the page's own "Download" button before it.
DOWNLOAD_DIALOG = 'dialog "Download Your Transaction Activity"'
FORMAT_FIELD = "Select Download Format"
CSV_CHOICE = ".csv"
DIALOG_BUTTON = "Download"
ACTION_TIMEOUT_SECONDS = 30
PAGE_LOAD_TIMEOUT_SECONDS = 60
DOWNLOAD_TIMEOUT_SECONDS = 180


def require_td_url(current: str) -> str:
    url = page_url(current)
    hostname = urlparse(url).hostname
    if hostname is None or not (
        hostname == TD_HOST or hostname.endswith(f".{TD_HOST}")
    ):
        raise RuntimeError("Refusing to fill credentials outside td.com")
    return url


def require_no_interactive_challenge(current: str) -> None:
    normalized = current.lower()
    if any(marker in normalized for marker in CHALLENGE_MARKERS):
        raise InteractiveChallengeError("TD requires an interactive security challenge")


def user_control(current: str) -> tuple[str, str] | None:
    """(role, ref) of the username field, a textbox or a saved-card combobox."""
    for role, name, ref in controls(current, ("textbox", "combobox")):
        if USER_FIELD.lower() in name.lower():
            return role, ref
    return None


def sign_on_form(current: str) -> tuple[tuple[str, str], str] | None:
    """((role, ref) of the user field, ref of the password) when this is sign-on."""
    user = user_control(current)
    password = find_control(current, (PASSWORD_FIELD,), ("textbox",))
    if user is None or password is None:
        return None
    return user, password[1]


def is_authenticated_page(current: str) -> bool:
    return sign_on_form(current) is None and AUTHENTICATED_MARKER in current


def last_four(value: str) -> str:
    return re.sub(r"\D", "", value)[-4:]


async def dismiss_privacy_dialog(
    session: BrowserOSSession, page: int, current: str
) -> bool:
    if PRIVACY_DIALOG not in current:
        return False
    found = find_control(current, (PRIVACY_CLOSE,), ("button",))
    if found is None:
        return False
    await click(session, page, found[1])
    return True


async def fill_login(
    session: BrowserOSSession,
    page: int,
    current: str,
    user: str,
    password: str,
) -> None:
    require_td_url(current)
    form = sign_on_form(current)
    if form is None:
        raise RuntimeError("TD page changed: the sign-on form is unavailable")
    (role, user_ref), password_ref = form
    if role == "combobox":
        # TD offers a remembered card; it must be the card in TD_USER.
        shown = displayed_value(current, user_ref) or ""
        if last_four(shown) != last_four(user):
            raise RuntimeError(
                "TD offers a different saved card than TD_USER; sign in by hand "
                "once with the right card, then rerun"
            )
    else:
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
    await act(session, page, "focus", ref=password_ref)
    await act(session, page, "press", key="Enter")


def send_alert(message: str) -> None:
    """Log the notice, and post it to the Discord alert bot when one is configured.

    A failed post is logged by exception type only, never raised: a missing
    notification must not abort a sign-in the person can still approve.
    """
    logger.warning(message)
    url = os.environ.get("DISCORD_ALERT_URL", "")
    if not url:
        return
    parsed = urlparse(url)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        logger.warning("DISCORD_ALERT_URL is not an http(s) URL; notice not posted")
        return
    request = urllib.request.Request(
        url,
        data=json.dumps({"message": message}).encode(),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=ALERT_TIMEOUT_SECONDS):
            pass
    except Exception as exc:
        logger.warning("Discord alert not delivered (%s)", type(exc).__name__)


async def authenticate(
    session: BrowserOSSession,
    page: int,
    user: str,
    password: str,
    *,
    timeout: float = LOGIN_TIMEOUT_SECONDS,
    poll_seconds: float = POLL_SECONDS,
    notify: Callable[[str], None] = send_alert,
) -> str:
    """Sign in when needed and return the authenticated page snapshot."""
    deadline = time.monotonic() + timeout
    credentials_sent_at: float | None = None
    waiting_since: float | None = None
    approval_notified = False
    while time.monotonic() < deadline:
        current = await snapshot(session, page)
        try:
            require_td_url(current)
        except RuntimeError as exc:
            if str(exc) != NO_ACTIVE_PAGE_ERROR:
                raise
            await asyncio.sleep(poll_seconds)
            continue
        require_no_interactive_challenge(current)
        if is_authenticated_page(current):
            return current
        if await dismiss_privacy_dialog(session, page, current):
            continue
        if sign_on_form(current) is not None:
            waiting_since = None
            if credentials_sent_at is None:
                await fill_login(session, page, current, user, password)
                credentials_sent_at = time.monotonic()
                continue
            if time.monotonic() - credentials_sent_at > LOGIN_REJECTED_AFTER_SECONDS:
                raise RuntimeError("TD rejected the login credentials")
        elif credentials_sent_at is not None and not approval_notified:
            waiting_since = waiting_since or time.monotonic()
            if time.monotonic() - waiting_since > APPROVAL_NOTICE_AFTER_SECONDS:
                approval_notified = True
                await asyncio.to_thread(notify, APPROVAL_NOTICE)
        await asyncio.sleep(poll_seconds)
    raise InteractiveChallengeError("TD did not complete sign-in before the timeout")


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
    raise RuntimeError(f"Timed out waiting for TD {description}")


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


def dialog_block(current: str) -> str:
    """The part of a snapshot from the download dialog onward, or empty."""
    index = current.find(DOWNLOAD_DIALOG)
    return "" if index < 0 else current[index:]


def form_date(value: date) -> str:
    return value.strftime("%m/%d/%Y")


def table_dates(current: str) -> list[date]:
    """The dates in the activity table's first column, as TD shows them."""
    return [
        datetime.strptime(text, TABLE_DATE_FORMAT).date()
        for text in TABLE_DATE_PATTERN.findall(current)
    ]


def require_history_window(since: date, today: date | None = None) -> None:
    today = today or date.today()
    if today - since > timedelta(days=MAX_HISTORY_DAYS):
        raise RuntimeError(
            "TD keeps about 18 months of activity; --since is older than that"
        )


def statement_periods(
    labels: list[str], today: date | None = None
) -> list[tuple[str, date, date]]:
    """(label, first day, last day) for each statement period label."""
    today = today or date.today()
    periods = []
    for label in labels:
        match = PERIOD_PATTERN.match(label)
        if match is None:
            continue
        start = datetime.strptime(match.group(1), PERIOD_DATE_FORMAT).date()
        end = (
            datetime.strptime(match.group(2), PERIOD_DATE_FORMAT).date()
            if match.group(2)
            else today
        )
        periods.append((label, start, end))
    return periods


def periods_overlapping(
    periods: list[tuple[str, date, date]], since: date, until: date
) -> list[tuple[str, date, date]]:
    """The periods touching since..until, oldest first."""
    return sorted(
        (period for period in periods if period[1] <= until and period[2] >= since),
        key=lambda period: period[1],
    )


def account_links(current: str, account: str) -> list[str]:
    markers = ACCOUNT_MARKERS[account]
    return [
        ref
        for _, name, ref in controls(current, ("link",))
        if any(marker in name.lower() for marker in markers)
    ]


async def open_account(
    session: BrowserOSSession, page: int, current: str, account: str
) -> str:
    listed = await wait_for(
        session,
        page,
        lambda c: bool(account_links(c, account)),
        f"the {account} account on the account list",
    )
    refs = account_links(listed, account)
    if len(refs) > 1:
        raise RuntimeError(f"TD lists {len(refs)} {account} accounts; cannot choose")
    await click(session, page, refs[0])
    if account == "debit":
        return await wait_for(
            session,
            page,
            lambda c: (
                control_named(c, QUICK_FILTERS, ("combobox",)) is not None
                and control_named(c, FILTER_BUTTON, ("button",)) is not None
            ),
            "the chequing activity page",
        )
    return await wait_for(
        session,
        page,
        lambda c: control_named(c, VISA_DOWNLOAD, ("button",)) is not None,
        "the Visa activity page",
        timeout=PAGE_LOAD_TIMEOUT_SECONDS,
    )


async def apply_date_range(
    session: BrowserOSSession, page: int, current: str, since: date, until: date
) -> str:
    await click(
        session,
        page,
        require_named(
            current,
            FILTER_BUTTON,
            ("button",),
            "TD page changed: the filter button is unavailable",
        ),
    )
    panel = await wait_for(
        session,
        page,
        lambda c: control_named(c, APPLY_FILTERS, ("button",)) is not None,
        "the date filter",
    )
    from_ref = require_named(
        panel, FROM_FIELD, ("textbox",), "TD page changed: no From date field"
    )
    to_ref = require_named(
        panel, TO_FIELD, ("textbox",), "TD page changed: no To date field"
    )
    await act(
        session,
        page,
        "fill",
        fields=[
            {"ref": from_ref, "value": form_date(since)},
            {"ref": to_ref, "value": form_date(until)},
        ],
    )
    await click(
        session,
        page,
        require_named(
            panel,
            APPLY_FILTERS,
            ("button",),
            "TD page changed: no Apply filters button",
        ),
    )

    def applied(c: str) -> bool:
        ref = control_named(c, QUICK_FILTERS, ("combobox",))
        if ref is None or displayed_value(c, ref) != CUSTOM_RANGE:
            return False
        # The table still shows the old range until the filter lands.
        dates = table_dates(c)
        return bool(dates) and all(since <= day <= until for day in dates)

    try:
        return await wait_for(session, page, applied, "the date range to apply")
    except RuntimeError as exc:
        raise RuntimeError("TD did not keep the requested date range") from exc


async def open_download_dialog(
    session: BrowserOSSession, page: int, current: str, button_name: str
) -> str:
    await click(
        session,
        page,
        require_named(
            current,
            button_name,
            ("button",),
            "TD page changed: the download button is unavailable",
        ),
    )
    return await wait_for(
        session,
        page,
        lambda c: (
            find_control(dialog_block(c), (FORMAT_FIELD,), ("combobox",)) is not None
        ),
        "the download dialog",
    )


def csv_option(current: str) -> str | None:
    for _, name, ref in controls(dialog_block(current), ("option",)):
        if CSV_CHOICE in name.lower():
            return ref
    return None


def format_is_csv(current: str) -> bool:
    block = dialog_block(current)
    found = find_control(block, (FORMAT_FIELD,), ("combobox",))
    if found is None:
        return False
    name, ref = found
    return (
        CSV_CHOICE in name.lower()
        or CSV_CHOICE in (displayed_value(block, ref) or "").lower()
    )


async def choose_csv(session: BrowserOSSession, page: int, current: str) -> str:
    if format_is_csv(current):
        return current
    found = find_control(dialog_block(current), (FORMAT_FIELD,), ("combobox",))
    if found is None:
        raise RuntimeError("TD page changed: the download format is unavailable")
    await click(session, page, found[1])
    opened = await wait_for(
        session,
        page,
        lambda c: csv_option(c) is not None,
        "the download format list",
    )
    await click(session, page, csv_option(opened))
    return await wait_for(session, page, format_is_csv, "the CSV format to be selected")


async def export_current(
    session: BrowserOSSession, page: int, current: str, workdir: Path, label: str
) -> Path:
    ref = require_named(
        dialog_block(current),
        DIALOG_BUTTON,
        ("button",),
        "TD page changed: the dialog download button is unavailable",
    )
    # The bank may take a while to build the file; the default tool budget can
    # expire first.
    downloaded = download_path(
        await session.call(
            "download",
            {"page": page, "ref": ref},
            timeout=DOWNLOAD_TIMEOUT_SECONDS,
        )
    )
    if downloaded.suffix.lower() != ".csv":
        raise RuntimeError(f"TD exported a non-CSV artifact: {downloaded.name}")
    # Every TD export is named accountactivity.csv, so move it before the next.
    return move_download(downloaded, workdir, f"{label}.csv")


async def export_debit(
    session: BrowserOSSession,
    page: int,
    accounts_page: str,
    since: date,
    until: date,
    workdir: Path,
) -> list[Path]:
    activity = await open_account(session, page, accounts_page, "debit")
    filtered = await apply_date_range(session, page, activity, since, until)
    dialog = await open_download_dialog(session, page, filtered, PAGE_DOWNLOAD)
    dialog = await choose_csv(session, page, dialog)
    return [await export_current(session, page, dialog, workdir, "debit-1")]


def period_options(current: str) -> list[tuple[str, str]]:
    """(label, ref) for each statement period offered in the open list."""
    return [
        (name, ref)
        for _, name, ref in controls(dialog_block(current), ("option",))
        if PERIOD_PATTERN.match(name)
    ]


async def open_period_list(session: BrowserOSSession, page: int, current: str) -> str:
    ref = require_named(
        dialog_block(current),
        PERIOD_FIELD,
        ("combobox",),
        "TD page changed: the statement period list is unavailable",
    )
    await click(session, page, ref)
    return await wait_for(
        session,
        page,
        lambda c: bool(period_options(c)),
        "the statement period list",
    )


def period_selected(current: str, label: str) -> bool:
    block = dialog_block(current)
    ref = control_named(block, PERIOD_FIELD, ("combobox",))
    return ref is not None and displayed_value(block, ref) == label


async def export_visa(
    session: BrowserOSSession,
    page: int,
    accounts_page: str,
    since: date,
    until: date,
    workdir: Path,
) -> list[Path]:
    activity = await open_account(session, page, accounts_page, "visa")
    dialog = await open_download_dialog(session, page, activity, VISA_DOWNLOAD)
    listing = await open_period_list(session, page, dialog)
    wanted = periods_overlapping(
        statement_periods([label for label, _ in period_options(listing)]),
        since,
        until,
    )
    if not wanted:
        raise RuntimeError(f"TD lists no statement period from {since} to {until}")
    files: list[Path] = []
    for index, (label, _, _) in enumerate(wanted, start=1):
        if index > 1:
            listing = await open_period_list(
                session, page, await snapshot(session, page)
            )
        await click(session, page, dict(period_options(listing))[label])
        current = await wait_for(
            session,
            page,
            lambda c, label=label: period_selected(c, label),
            f"the {label} period to be selected",
        )
        current = await choose_csv(session, page, current)
        files.append(
            await export_current(session, page, current, workdir, f"visa-{index}")
        )
    return files


async def sign_out(session: BrowserOSSession, page: int) -> None:
    """Best effort: leave no signed-in parents session behind."""
    try:
        current = await snapshot(session, page)
        ref = control_named(current, LOGOUT_LINK, ("link",))
        if ref is not None:
            await click(session, page, ref)
            await asyncio.sleep(2)
    except RuntimeError:
        pass


async def run_download(
    account: str,
    since: date,
    until: date,
    user: str,
    password: str,
    *,
    session_factory: BrowserOSSessionFactory = FastMCPBrowserOSSession,
) -> list[Path]:
    """Sign in, export the account's activity, and return the raw CSV paths.

    The files sit in a private temporary folder the caller must remove. On any
    failure the folder is removed here.
    """
    if account not in ACCOUNT_MARKERS:
        raise ValueError(f"unknown TD account: {account}")
    require_history_window(since)
    workdir = Path(tempfile.mkdtemp(prefix="td-export-"))
    try:
        async with session_factory(browseros_mcp_url()) as session:
            page = page_from_response(
                await session.call("tabs", {"action": "new", "url": LOGIN_URL})
            )
            try:
                accounts_page = await authenticate(session, page, user, password)
                export = export_debit if account == "debit" else export_visa
                files = await export(
                    session,
                    page,
                    accounts_page,
                    since,
                    min(until, date.today()),
                    workdir,
                )
                logger.info("TD exported %s activity (%s file(s))", account, len(files))
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

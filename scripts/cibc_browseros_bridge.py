#!/usr/bin/env python3
"""CIBC BrowserOS bridge. UI facts verified 2026-11-12; see private spec."""

import asyncio
import calendar
import logging
import os
import re
import select
import shutil
import sys
import tempfile
import termios
import time
from datetime import date
from pathlib import Path
from urllib.parse import urlparse

from scripts.amex_browseros_bridge import (
    FastMCPBrowserOSSession,
    browseros_mcp_url,
    click,
    download_path,
    page_from_response,
    snapshot,
)
from scripts.browser_common import move_download
from scripts.rogers_browseros_bridge import control_named
from scripts.simplii_browseros_bridge import act, controls, find_control, page_url
from scripts.td_browseros_bridge import send_alert


class CibcError(RuntimeError):
    """Actionable, secret-free failure safe for the CLI to display."""


logger = logging.getLogger("cibc_browseros_bridge")
LOGIN_URL = "https://www.cibconline.cibc.com/ebm-resources/public/auth-gateway/main/client/index.html?locale=en&auth_context=login"
EXPORT_URL = "https://www.cibconline.cibc.com/ebm-resources/public/banking/cibc/client/web/index.html#/accounts/download"
LOGIN_HOST = "secure.cibc.com"
EXPORT_HOST = "www.cibconline.cibc.com"


def require_origin(current: str, *, secret: bool = False) -> str:
    url = page_url(current)
    parsed = urlparse(url)
    allowed = (LOGIN_HOST,) if secret else (LOGIN_HOST, EXPORT_HOST)
    if (
        parsed.scheme != "https"
        or parsed.hostname not in allowed
        or parsed.port not in (None, 443)
    ):
        raise CibcError("Refusing CIBC action outside verified HTTPS origin")
    return parsed.hostname


def check_page(current: str) -> None:
    require_origin(current)
    normalized = current.lower()
    if any(
        text in normalized
        for text in (
            "captcha",
            "verify you are human",
            "incorrect code",
            "invalid code",
            "code has expired",
            "unable to",
            "technical error",
            "date range is invalid",
            "not available for",
            "use a different contact method",
        )
    ):
        raise CibcError(
            "CIBC security challenge or site error; inspect the page and rerun"
        )


def read_verification_code(deadline: float, *, fd: int | None = None) -> str:
    """Read hidden SSH terminal input; select bounds even blocked input."""
    try:
        fd = sys.stdin.fileno() if fd is None else fd
    except (OSError, ValueError):
        raise CibcError(
            "CIBC MFA requires an interactive SSH terminal; no echo fallback"
        ) from None
    if not os.isatty(fd):
        raise CibcError(
            "CIBC MFA requires an interactive SSH terminal; no echo fallback"
        )
    previous = termios.tcgetattr(fd)
    hidden = termios.tcgetattr(fd)
    hidden[3] &= ~(termios.ECHO | termios.ECHONL)
    pending = b""
    try:
        termios.tcsetattr(fd, termios.TCSANOW, hidden)
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise CibcError("Timed out waiting for CIBC verification code; rerun")
            print("CIBC verification code: ", end="", file=sys.stderr, flush=True)
            while b"\n" not in pending:
                remaining = deadline - time.monotonic()
                if remaining <= 0 or not select.select([fd], [], [], remaining)[0]:
                    raise CibcError(
                        "Timed out waiting for CIBC verification code; rerun"
                    )
                chunk = os.read(fd, 1024)
                if not chunk:
                    raise CibcError("CIBC verification input ended; rerun")
                pending += chunk
            line, pending = pending.split(b"\n", 1)
            print(file=sys.stderr)
            code = line.decode("ascii", errors="replace").strip()
            if re.fullmatch(r"[0-9]{6}", code):
                if time.monotonic() >= deadline:
                    raise CibcError(
                        "Timed out waiting for CIBC verification code; rerun"
                    )
                return code
            print("Enter exactly six ASCII digits.", file=sys.stderr)
    except BaseException:
        termios.tcflush(fd, termios.TCIFLUSH)
        raise
    finally:
        termios.tcsetattr(fd, termios.TCSANOW, previous)


def required(current: str, name: str, roles=("button",)) -> str:
    ref = control_named(current, name, roles)
    if ref is None:
        raise CibcError(f"CIBC page changed: {name} unavailable")
    return ref


async def wait_for(session, page, predicate, description, *, timeout=30):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        current = await snapshot(session, page)
        check_page(current)
        if predicate(current):
            return current
        await asyncio.sleep(0.5)
    raise CibcError(f"Timed out waiting for CIBC {description}")


def code_field(current: str):
    return find_control(
        current, ("verification code", "6-digit code", "six-digit code"), ("textbox",)
    )


def authenticated(current: str) -> bool:
    return any(
        name.lower() in ("sign out", "sign off") for _, name, _ in controls(current)
    )


async def authenticate(session, page, user: str, password: str) -> None:
    deadline = time.monotonic() + 300
    user_sent = password_sent = code_sent = False
    while time.monotonic() < deadline:
        current = await snapshot(session, page)
        check_page(current)
        if authenticated(current):
            return
        field = code_field(current)
        if field:
            if code_sent:
                raise CibcError(
                    "CIBC rejected or expired the code; rerun without resending"
                )
            mfa_deadline = time.monotonic() + 180
            require_origin(current, secret=True)
            if not os.environ.get("DISCORD_ALERT_URL"):
                logger.warning("DISCORD_ALERT_URL missing; reminder not delivered")
            send_alert(
                "CIBC sign-in needs your push verification code. Enter it in the SSH terminal, not Discord."
            )
            code = read_verification_code(mfa_deadline)
            current = await snapshot(session, page)
            require_origin(current, secret=True)
            field = code_field(current)
            if field is None or time.monotonic() >= mfa_deadline:
                raise CibcError("CIBC verification expired; rerun")
            await act(
                session,
                page,
                "fill",
                fields=[{"ref": field[1], "value": code}],
                sensitive=True,
            )
            del code
            current = await snapshot(session, page)
            require_origin(current, secret=True)
            button = find_control(
                current, ("Continue", "Verify", "Submit"), ("button",)
            )
            if button is None:
                raise CibcError("CIBC verification submit unavailable")
            await click(session, page, button[1])
            code_sent = True
            # Allow the submitted screen to disappear before checking rejection.
            await wait_for(
                session,
                page,
                lambda c: code_field(c) is None,
                "code acceptance",
                timeout=20,
            )
            continue
        field = find_control(current, ("Card number",), ("textbox",))
        if field and not user_sent:
            require_origin(current, secret=True)
            await act(
                session,
                page,
                "fill",
                fields=[{"ref": field[1], "value": user}],
                sensitive=True,
            )
            current = await snapshot(session, page)
            require_origin(current, secret=True)
            await click(session, page, required(current, "Continue"))
            user_sent = True
            continue
        field = find_control(current, ("Password (case sensitive)",), ("textbox",))
        if field and not password_sent:
            require_origin(current, secret=True)
            await act(
                session,
                page,
                "fill",
                fields=[{"ref": field[1], "value": password}],
                sensitive=True,
            )
            current = await snapshot(session, page)
            require_origin(current, secret=True)
            await act(session, page, "press", key="Escape")
            await act(session, page, "focus", ref=required(current, "Sign on"))
            await act(session, page, "press", key="Enter")
            password_sent = True
            continue
        await asyncio.sleep(0.5)
    raise CibcError(
        "CIBC sign-in timed out; check credentials or unknown verification challenge and rerun"
    )


def mastercard_option(current: str) -> str:
    matches = [
        name
        for _, name, _ in controls(current, ("option",))
        if "cibc mastercard" in name.lower()
    ]
    if len(matches) != 1:
        raise CibcError(
            "CIBC requires exactly one eligible Mastercard; found zero or multiple cards"
        )
    return matches[0]


async def export_activity(
    session, page, since: date, until: date, workdir: Path
) -> list[Path]:
    await session.call("navigate", {"action": "url", "page": page, "url": EXPORT_URL})
    current = await wait_for(
        session,
        page,
        lambda c: control_named(c, "Account:", ("combobox",)) is not None,
        "export form",
    )
    if require_origin(current) != EXPORT_HOST:
        raise CibcError("CIBC export requires authenticated banking origin")
    option = mastercard_option(current)
    await act(
        session,
        page,
        "select",
        ref=required(current, "Account:", ("combobox",)),
        value=option,
    )
    current = await snapshot(session, page)
    check_page(current)
    # The native radio is covered; its visible label is the verified click target.
    await click(session, page, required(current, "All within date range", ("generic",)))
    for prefix, value in (("From", since), ("To", until)):
        for part, label in (
            ("month", calendar.month_name[value.month]),
            ("day", str(value.day)),
            ("year", str(value.year)),
        ):
            current = await snapshot(session, page)
            check_page(current)
            await act(
                session,
                page,
                "select",
                ref=required(current, f"{prefix} {part}", ("combobox",)),
                value=label,
            )
    current = await snapshot(session, page)
    check_page(current)
    await act(
        session,
        page,
        "select",
        ref=required(current, "Financial management software:", ("combobox",)),
        value="Spreadsheet (CSV)",
    )
    current = await snapshot(session, page)
    check_page(current)
    downloaded = download_path(
        await session.call(
            "download",
            {"page": page, "ref": required(current, "Download Transactions")},
            timeout=180,
        )
    )
    if downloaded.suffix.lower() != ".csv":
        raise CibcError("CIBC exported a non-CSV artifact")
    return [move_download(downloaded, workdir, "cibc.csv")]


async def run_download(
    since: date,
    until: date,
    user: str,
    password: str,
    *,
    session_factory=FastMCPBrowserOSSession,
) -> list[Path]:
    if since < date(2011, 12, 11) or until < since:
        raise ValueError("Invalid CIBC history range")
    workdir = Path(tempfile.mkdtemp(prefix="cibc-export-"))
    try:
        async with session_factory(browseros_mcp_url()) as session:
            page = page_from_response(
                await session.call("tabs", {"action": "new", "url": LOGIN_URL})
            )
            try:
                await authenticate(session, page, user, password)
                return await export_activity(session, page, since, until, workdir)
            finally:
                try:
                    current = await snapshot(session, page)
                    require_origin(current)
                    button = find_control(
                        current, ("Sign out", "Sign off"), ("button", "link")
                    )
                    if button:
                        await click(session, page, button[1])
                except Exception:
                    logger.warning("CIBC sign-out could not be confirmed")
                finally:
                    try:
                        await session.call("tabs", {"action": "close", "page": page})
                    except Exception:
                        logger.warning("CIBC task tab could not be closed")
    except BaseException:
        shutil.rmtree(workdir, ignore_errors=True)
        raise

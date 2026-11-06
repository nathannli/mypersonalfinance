#!/usr/bin/env python3
"""Local MCP bridge for TD EasyWeb account-activity downloads.

Only the sign-in is implemented so far. It drives the BrowserOS-managed profile
in a new tab, fills credentials only after the page origin is confirmed to be a
td.com host, and ends the run on a CAPTCHA instead of working around it. TD's
two-step verification is a push approval in the TD app, which a person must give:
the bridge posts one notice to the Discord alert bot and then waits for the
signed-in page.

Every TD screen fact lives in the VERIFIED UI FACTS block below and nowhere else.
"""

import asyncio
import json
import logging
import os
import re
import time
import urllib.request
from collections.abc import Callable
from urllib.parse import urlparse

from scripts.amex_browseros_bridge import BrowserOSSession, click, snapshot
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


def credentials() -> tuple[str, str]:
    values = tuple(os.environ.get(name, "") for name in ("TD_USER", "TD_PASSWORD"))
    if not all(values):
        raise RuntimeError("TD_USER and TD_PASSWORD must be configured locally")
    return values


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

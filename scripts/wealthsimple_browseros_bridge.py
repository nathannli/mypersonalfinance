"""Local MCP bridge for unattended Wealthsimple activity scrapes through BrowserOS neo."""

import asyncio
import base64
import hashlib
import hmac
import json
import re
import struct
import time
from datetime import date, datetime
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

from scripts.amex_browseros_bridge import (
    BrowserOSSession,
    BrowserOSSessionFactory,
    FastMCPBrowserOSSession,
    browseros_mcp_url,
    click,
    page_from_response,
    response_text,
    snapshot,
)

BASE_URL = "https://my.wealthsimple.com"
WEALTHSIMPLE_HOST = "wealthsimple.com"
LOGIN_TIMEOUT_SECONDS = 5 * 60
ACTION_TIMEOUT_SECONDS = 30
TOTP_STEP_SECONDS = 30
TOTP_MIN_REMAINING_SECONDS = 3
MAX_TOTP_ATTEMPTS = 2
MAX_LOAD_MORE_CLICKS = 200
EVALUATE_MAX_CHARS = 5_000_000
LOGIN_REJECTED_AFTER_SECONDS = 20
CHALLENGE_MARKERS = (
    "captcha",
    "text message",
    "sms",
    "approve this login",
    "check your phone",
    "check your email",
)

# Read-only: returns the visible text of the activity list and nothing else.
# Rows are buttons whose last leaf is an amount; each row belongs to the
# nearest preceding date heading.
ROWS_SCRIPT = r"""
return (() => {
  const main = document.querySelector('main');
  if (!main) return JSON.stringify({rows: [], loadMore: false});
  const isDate = (s) => /^[A-Z][a-z]+ \d{1,2}, \d{4}$/.test(s);
  const nodes = main.querySelectorAll('h1,h2,h3,h4,[role="heading"],button');
  const rows = [];
  let heading = null;
  let loadMore = false;
  for (const node of nodes) {
    const text = node.textContent.trim();
    if (node.tagName !== 'BUTTON') {
      heading = text;
      continue;
    }
    if (text === 'Load more') { loadMore = true; continue; }
    const leaves = [...node.querySelectorAll('*')]
      .filter((e) => e.children.length === 0 && e.textContent.trim())
      .map((e) => e.textContent.trim());
    if (!leaves.length || !/\$[\d,]+\.\d{2}\s+[A-Z]{3}$/.test(leaves[leaves.length - 1])) continue;
    rows.push({heading: heading, date: isDate(heading || '') ? heading : null, spans: leaves});
  }
  return JSON.stringify({rows: rows, loadMore: loadMore});
})()
"""


class InteractiveChallengeError(RuntimeError):
    pass


def totp_code(secret: str, at: float | None = None) -> str:
    key = re.sub(r"[\s-]", "", secret).upper()
    key = base64.b32decode(key + "=" * (-len(key) % 8))
    counter = int((time.time() if at is None else at) // TOTP_STEP_SECONDS)
    digest = hmac.new(key, struct.pack(">Q", counter), hashlib.sha1).digest()
    offset = digest[-1] & 0x0F
    value = struct.unpack(">I", digest[offset : offset + 4])[0] & 0x7FFFFFFF
    return f"{value % 1_000_000:06d}"


def page_url(current: str) -> str:
    match = re.match(
        r"^\[UNTRUSTED_PAGE_CONTENT\b[^\]]*\borigin=(https://[^\s\]]+)", current
    )
    if match is None:
        raise RuntimeError("BrowserOS did not return the active page URL")
    return match.group(1)


def require_wealthsimple_url(current: str) -> str:
    url = page_url(current)
    hostname = urlparse(url).hostname
    if hostname is None or not (
        hostname == WEALTHSIMPLE_HOST or hostname.endswith(f".{WEALTHSIMPLE_HOST}")
    ):
        raise RuntimeError("Refusing to fill credentials outside wealthsimple.com")
    return url


def account_url(link: str) -> str:
    if not link:
        raise RuntimeError("Wealthsimple account link is missing from .env")
    return f"{BASE_URL}/{link.lstrip('/')}"


def account_ids(url: str) -> list[str]:
    return parse_qs(urlparse(url).query).get("account_ids", [])


def is_activity_page(current: str, target: str) -> bool:
    url = page_url(current)
    return urlparse(url).path.rstrip("/") == "/app/activity" and account_ids(
        url
    ) == account_ids(target)


def refs_for_role(current: str, role: str) -> list[str]:
    return re.findall(rf"- {re.escape(role)}\b[^\n]*\[ref=(e\d+)\]", current)


def button_ref(current: str, name: str) -> str | None:
    match = re.search(
        rf'- button "{re.escape(name)}"(?![^\n]*\[disabled\])[^\n]*\[ref=(e\d+)\]',
        current,
    )
    return match.group(1) if match else None


def require_no_interactive_challenge(current: str) -> None:
    normalized = current.lower()
    if any(marker in normalized for marker in CHALLENGE_MARKERS):
        raise InteractiveChallengeError(
            "Wealthsimple requires an interactive security challenge"
        )


async def fill(session: BrowserOSSession, page: int, fields: list[tuple[str, str]]):
    await session.call(
        "act",
        {
            "page": page,
            "kind": "fill",
            "fields": [{"ref": ref, "value": value} for ref, value in fields],
        },
    )


async def submit_totp(
    session: BrowserOSSession, page: int, current: str, secret: str
) -> int:
    remaining = TOTP_STEP_SECONDS - time.time() % TOTP_STEP_SECONDS
    if remaining < TOTP_MIN_REMAINING_SECONDS:
        await asyncio.sleep(remaining + 0.5)
    step = int(time.time() // TOTP_STEP_SECONDS)
    textboxes = refs_for_role(current, "textbox")
    if len(textboxes) != 1:
        raise RuntimeError("Wealthsimple page changed: TOTP code field is unavailable")
    await fill(session, page, [(textboxes[0], totp_code(secret))])
    await asyncio.sleep(1)
    after = await snapshot(session, page)
    if (ref := button_ref(after, "Submit")) is not None:
        await click(session, page, ref)
    return step


async def authenticate(
    session: BrowserOSSession,
    page: int,
    target: str,
    email: str,
    password: str,
    totp_secret: str,
    *,
    timeout: float = LOGIN_TIMEOUT_SECONDS,
    poll_seconds: float = 1.0,
) -> None:
    deadline = time.monotonic() + timeout
    credentials_sent_at: float | None = None
    totp_steps: list[int] = []
    while time.monotonic() < deadline:
        current = await snapshot(session, page)
        try:
            require_wealthsimple_url(current)
        except RuntimeError as exc:
            if str(exc) != "BrowserOS did not return the active page URL":
                raise
            await asyncio.sleep(poll_seconds)
            continue
        if is_activity_page(current, target):
            return
        if "Check your authenticator" in current:
            step = int(time.time() // TOTP_STEP_SECONDS)
            if not totp_steps or step != totp_steps[-1]:
                if len(totp_steps) >= MAX_TOTP_ATTEMPTS:
                    raise RuntimeError("Wealthsimple rejected the TOTP code twice")
                totp_steps.append(
                    await submit_totp(session, page, current, totp_secret)
                )
                continue
        elif (ref := button_ref(current, "Try another way")) is not None:
            await click(session, page, ref)
            continue
        elif credentials_sent_at is None and (ref := button_ref(current, "Log in")):
            textboxes = refs_for_role(current, "textbox")
            if len(textboxes) < 2:
                raise RuntimeError(
                    "Wealthsimple page changed: login fields are unavailable"
                )
            require_wealthsimple_url(current)
            await fill(session, page, [(textboxes[0], email), (textboxes[1], password)])
            await click(session, page, ref)
            credentials_sent_at = time.monotonic()
            continue
        elif (
            credentials_sent_at is not None
            and button_ref(current, "Log in")
            and time.monotonic() - credentials_sent_at > LOGIN_REJECTED_AFTER_SECONDS
        ):
            raise RuntimeError("Wealthsimple rejected the login credentials")
        require_no_interactive_challenge(current)
        await asyncio.sleep(poll_seconds)
    raise InteractiveChallengeError(
        "Wealthsimple requires an interactive security challenge"
    )


async def read_rows(session: BrowserOSSession, page: int) -> dict[str, Any]:
    text = response_text(
        await session.call(
            "evaluate",
            {"page": page, "code": ROWS_SCRIPT, "maxChars": EVALUATE_MAX_CHARS},
        )
    )
    match = re.search(r"\{.*\}", text, re.DOTALL)
    if match is None:
        raise RuntimeError("BrowserOS returned unreadable Wealthsimple activity")
    return json.loads(match.group(0))


def parse_heading_date(heading: str) -> date:
    return datetime.strptime(heading, "%B %d, %Y").date()


def oldest_date(rows: list[dict[str, Any]]) -> date | None:
    dates = [parse_heading_date(row["date"]) for row in rows if row["date"]]
    return min(dates) if dates else None


async def load_until(
    session: BrowserOSSession,
    page: int,
    since: date,
    *,
    max_clicks: int = MAX_LOAD_MORE_CLICKS,
    timeout: float = ACTION_TIMEOUT_SECONDS,
    poll_seconds: float = 0.5,
) -> list[dict[str, Any]]:
    clicks = 0
    deadline = time.monotonic() + timeout
    while True:
        state = await read_rows(session, page)
        if state["rows"] or state["loadMore"]:
            break
        if time.monotonic() >= deadline:
            raise RuntimeError("Timed out waiting for Wealthsimple activity to load")
        await asyncio.sleep(poll_seconds)
    while True:
        rows = state["rows"]
        oldest = oldest_date(rows)
        if not state["loadMore"] or (oldest is not None and oldest < since):
            return rows
        if clicks >= max_clicks:
            raise RuntimeError(
                f"Reached {max_clicks} Load more clicks before {since.isoformat()}"
            )
        ref = button_ref(await snapshot(session, page), "Load more")
        if ref is None:
            raise RuntimeError("Wealthsimple page changed: Load more is unavailable")
        await click(session, page, ref)
        clicks += 1
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            await asyncio.sleep(poll_seconds)
            grown = await read_rows(session, page)
            if len(grown["rows"]) > len(rows) or not grown["loadMore"]:
                break
        else:
            raise RuntimeError("Timed out waiting for more Wealthsimple activity")
        state = grown


def transactions(rows: list[dict[str, Any]], since: date) -> list[dict[str, str]]:
    result = []
    for row in rows:
        if row["date"] is None:
            continue
        row_date = parse_heading_date(row["date"])
        if row_date < since:
            continue
        spans = row["spans"]
        if len(spans) < 3 or not spans[0] or not spans[1]:
            raise RuntimeError("Wealthsimple page changed: activity row is incomplete")
        amount = spans[-1]
        if not amount.endswith(" CAD"):
            raise RuntimeError("Wealthsimple activity has a non-CAD row")
        result.append(
            {
                "description": spans[0],
                "type": spans[1],
                "amount": amount,
                "date": row_date.isoformat(),
            }
        )
    return result


async def run_scrape(
    link: str,
    since: date,
    email: str,
    password: str,
    totp_secret: str,
    *,
    session_factory: BrowserOSSessionFactory = FastMCPBrowserOSSession,
) -> list[dict[str, str]]:
    target = account_url(link)
    async with session_factory(browseros_mcp_url()) as session:
        page = page_from_response(
            await session.call("tabs", {"action": "new", "url": target})
        )
        try:
            await authenticate(session, page, target, email, password, totp_secret)
            rows = await load_until(session, page, since)
        finally:
            try:
                await session.call("tabs", {"action": "close", "page": page})
            except RuntimeError:
                pass
    return transactions(rows, since)


def write_csv(rows: list[dict[str, str]], destination: Path) -> Path:
    import csv
    import os
    import tempfile

    destination = destination.expanduser().resolve()
    destination.parent.mkdir(parents=True, exist_ok=True)
    if destination.exists():
        raise FileExistsError(f"Refusing to overwrite existing file: {destination}")
    handle, temporary = tempfile.mkstemp(
        prefix=f".{destination.name}.", suffix=".tmp", dir=destination.parent
    )
    try:
        with os.fdopen(handle, "w", newline="", encoding="utf-8") as output:
            writer = csv.DictWriter(
                output, fieldnames=["description", "type", "amount", "date"]
            )
            writer.writeheader()
            writer.writerows(rows)
        try:
            os.link(temporary, destination)
        except FileExistsError:
            raise FileExistsError(
                f"Refusing to overwrite existing file: {destination}"
            ) from None
    finally:
        Path(temporary).unlink(missing_ok=True)
    return destination

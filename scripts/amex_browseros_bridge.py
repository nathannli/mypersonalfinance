"""Local MCP bridge for unattended Amex activity downloads through BrowserOS neo."""

import asyncio
import os
import re
import socket
import subprocess
import time
from contextlib import AbstractAsyncContextManager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Protocol
from urllib.parse import urlparse

from dotenv import load_dotenv
from fastmcp import Client, FastMCP

from scripts.download_amex_transactions import (
    ACTION_TIMEOUT_SECONDS,
    move_download,
    validate_download,
    wait_for_amex_sms_code,
)

load_dotenv()

LOGIN_URL = "https://www.americanexpress.com/en-ca/account/login/"
AUTHENTICATED_URL = "https://global.americanexpress.com/dashboard"
AMEX_HOST_SUFFIX = "americanexpress.com"
DEFAULT_BROWSEROS_MCP_URL = "http://127.0.0.1:9010/mcp"
MFA_TIMEOUT_SECONDS = 2 * 60
BROWSEROS_START_TIMEOUT_SECONDS = 30

mcp = FastMCP("Amex BrowserOS Bridge")


class BrowserOSSession(Protocol):
    async def call(self, tool_name: str, arguments: dict[str, Any]) -> Any: ...


class BrowserOSSessionFactory(Protocol):
    def __call__(
        self, endpoint: str
    ) -> AbstractAsyncContextManager[BrowserOSSession]: ...


class FastMCPBrowserOSSession:
    """Small adapter that keeps BrowserOS MCP responses out of bridge callers."""

    def __init__(self, endpoint: str):
        self.endpoint = endpoint
        self.client = Client(
            endpoint,
            timeout=ACTION_TIMEOUT_SECONDS,
            init_timeout=ACTION_TIMEOUT_SECONDS,
        )

    async def __aenter__(self) -> "FastMCPBrowserOSSession":
        await asyncio.to_thread(ensure_browseros_mcp, self.endpoint)
        await self.client.__aenter__()
        return self

    async def __aexit__(self, *args: object) -> None:
        await self.client.__aexit__(*args)

    async def call(self, tool_name: str, arguments: dict[str, Any]) -> Any:
        try:
            result = await self.client.call_tool(tool_name, arguments)
        except Exception as exc:
            raise RuntimeError("BrowserOS MCP call failed") from exc
        return result.data if result.data is not None else result.content


def browseros_mcp_url() -> str:
    endpoint = os.environ.get("BROWSEROS_MCP_URL", DEFAULT_BROWSEROS_MCP_URL)
    parsed = urlparse(endpoint)
    if (
        parsed.scheme not in {"http", "https"}
        or parsed.hostname not in {"127.0.0.1", "localhost", "::1"}
        or parsed.username
        or parsed.password
        or parsed.query
        or parsed.fragment
    ):
        raise RuntimeError("BROWSEROS_MCP_URL must be a loopback HTTP(S) URL")
    return endpoint


def endpoint_listening(endpoint: str) -> bool:
    parsed = urlparse(endpoint)
    host = parsed.hostname
    if host is None:
        return False
    port = parsed.port or (443 if parsed.scheme == "https" else 80)
    try:
        with socket.create_connection((host, port), timeout=1):
            return True
    except OSError:
        return False


def ensure_browseros_mcp(endpoint: str) -> None:
    if endpoint_listening(endpoint):
        return
    try:
        subprocess.run(
            ["open", "-a", "BrowserOS neo"],
            check=True,
            timeout=10,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise RuntimeError("Could not start BrowserOS neo") from exc
    deadline = time.monotonic() + BROWSEROS_START_TIMEOUT_SECONDS
    while time.monotonic() < deadline:
        if endpoint_listening(endpoint):
            return
        time.sleep(0.5)
    raise RuntimeError(
        f"BrowserOS MCP is not listening at {endpoint} after starting BrowserOS neo"
    )


def credentials() -> tuple[str, str]:
    user = os.environ.get("AMEX_USER")
    password = os.environ.get("AMEX_PASSWORD")
    if not user or not password:
        raise RuntimeError("AMEX_USER and AMEX_PASSWORD must be configured locally")
    return user, password


def response_text(response: Any) -> str:
    if isinstance(response, str):
        return response
    text = getattr(response, "text", None)
    if isinstance(text, str):
        return text
    if isinstance(response, dict):
        text = response.get("text")
        if isinstance(text, str):
            return text
        return "\n".join(response_text(value) for value in response.values())
    if isinstance(response, list):
        return "\n".join(response_text(value) for value in response)
    return str(response)


def page_from_response(response: Any) -> int:
    match = re.search(r"\bpage\s+(\d+)\b", response_text(response), re.IGNORECASE)
    if match is None:
        raise RuntimeError("BrowserOS did not return a task-owned page")
    return int(match.group(1))


def ref_for(snapshot: str, role: str, name: str) -> str:
    pattern = rf'- {re.escape(role)} "{re.escape(name)}"[^\n]*\[ref=(e\d+)\]'
    match = re.search(pattern, snapshot)
    if match is None:
        raise RuntimeError(f"Amex page changed: {role} {name!r} is unavailable")
    return match.group(1)


def ref_for_roles(snapshot: str, name: str, roles: tuple[str, ...]) -> str:
    for role in roles:
        try:
            return ref_for(snapshot, role, name)
        except RuntimeError:
            continue
    raise RuntimeError(f"Amex page changed: {name!r} is unavailable")


def ref_for_names(snapshot: str, role: str, names: tuple[str, ...]) -> str:
    for name in names:
        try:
            return ref_for(snapshot, role, name)
        except RuntimeError:
            continue
    raise RuntimeError("Amex page changed: statement activity link is unavailable")


def first_ref_for_role(snapshot: str, role: str) -> str:
    match = re.search(rf"- {re.escape(role)} [^\n]*\[ref=(e\d+)\]", snapshot)
    if match is None:
        raise RuntimeError(f"Amex page changed: no {role} is available")
    return match.group(1)


def amex_url(snapshot: str) -> str:
    match = re.search(r"origin=(https://[^\s\]]+)", snapshot)
    if match is None:
        raise RuntimeError("BrowserOS did not return the active page URL")
    return match.group(1)


def require_amex_url(snapshot: str) -> str:
    url = amex_url(snapshot)
    hostname = urlparse(url).hostname
    if hostname is None or not (
        hostname == AMEX_HOST_SUFFIX or hostname.endswith(f".{AMEX_HOST_SUFFIX}")
    ):
        raise RuntimeError("Refusing to fill credentials outside americanexpress.com")
    return url


def is_activity_page(snapshot: str) -> bool:
    return urlparse(amex_url(snapshot)).path.rstrip("/") == "/activity"


def download_path(response: Any) -> Path:
    if isinstance(response, dict):
        for key in ("path", "download_path"):
            value = response.get(key)
            if isinstance(value, str):
                return Path(value)
    match = re.search(r"(/[\w./ -]+\.(?:csv|CSV))\b", response_text(response))
    if match is None:
        raise RuntimeError("BrowserOS did not return a downloaded CSV path")
    return Path(match.group(1))


async def snapshot(session: BrowserOSSession, page: int) -> str:
    return response_text(await session.call("snapshot", {"page": page}))


async def wait_for_snapshot(
    session: BrowserOSSession, page: int, expected_text: str, timeout: int
) -> str:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        current = await snapshot(session, page)
        if expected_text in current:
            return current
        await asyncio.sleep(0.5)
    raise RuntimeError(f"Timed out waiting for Amex {expected_text!r}")


async def click(session: BrowserOSSession, page: int, ref: str) -> None:
    await session.call("act", {"page": page, "kind": "click", "ref": ref})


async def fill_login(
    session: BrowserOSSession, page: int, login_snapshot: str, user: str, password: str
) -> None:
    require_amex_url(login_snapshot)
    await session.call(
        "act",
        {
            "page": page,
            "kind": "fill",
            "fields": [
                {"ref": ref_for(login_snapshot, "textbox", "User ID"), "value": user},
                {
                    "ref": ref_for(login_snapshot, "textbox", "Password"),
                    "value": password,
                },
            ],
        },
    )
    await click(session, page, ref_for(login_snapshot, "button", "Log In"))


def require_no_interactive_challenge(current: str) -> None:
    challenge_markers = ("captcha", "security verification", "verify your identity")
    if any(marker in current.lower() for marker in challenge_markers):
        raise RuntimeError("Amex requires an interactive security challenge")


async def complete_sms_mfa(session: BrowserOSSession, page: int, delivery: str) -> None:
    await click(session, page, first_ref_for_role(delivery, "radio"))
    requested_at = datetime.now(timezone.utc).replace(tzinfo=None)
    await click(session, page, ref_for(delivery, "button", "Continue"))
    code_page = await wait_for_snapshot(
        session, page, "Please enter the verification code", ACTION_TIMEOUT_SECONDS
    )
    code = await asyncio.to_thread(wait_for_amex_sms_code, requested_at)
    await session.call(
        "act",
        {
            "page": page,
            "kind": "fill",
            "ref": first_ref_for_role(code_page, "textbox"),
            "value": code,
        },
    )
    await click(session, page, ref_for(code_page, "button", "Continue"))


async def authenticate(
    session: BrowserOSSession,
    page: int,
    user: str,
    password: str,
    login_snapshot: str | None = None,
) -> None:
    if login_snapshot is None:
        login_snapshot = await wait_for_snapshot(
            session, page, 'textbox "User ID"', ACTION_TIMEOUT_SECONDS
        )
    await fill_login(session, page, login_snapshot, user, password)
    deadline = time.monotonic() + MFA_TIMEOUT_SECONDS
    while time.monotonic() < deadline:
        current = await snapshot(session, page)
        require_no_interactive_challenge(current)
        require_amex_url(current)
        if is_authenticated_page(current):
            return
        if "Select how you'd like to receive your code" in current:
            await complete_sms_mfa(session, page, current)
            break
        await asyncio.sleep(0.5)
    else:
        raise RuntimeError("Timed out waiting for Amex authentication")

    deadline = time.monotonic() + MFA_TIMEOUT_SECONDS
    while time.monotonic() < deadline:
        authenticated = await snapshot(session, page)
        require_no_interactive_challenge(authenticated)
        require_amex_url(authenticated)
        if is_authenticated_page(authenticated):
            return
        await asyncio.sleep(0.5)
    raise RuntimeError("Timed out waiting for Amex authentication after SMS MFA")


def is_authenticated_page(current: str) -> bool:
    return urlparse(amex_url(current)).hostname == "global.americanexpress.com"


def is_dashboard_ready(current: str) -> bool:
    if not is_authenticated_page(current):
        return False
    try:
        ref_for_roles(current, "Statement", ("link", "button"))
    except RuntimeError:
        return False
    return True


async def authenticate_if_needed(
    session: BrowserOSSession, page: int, user: str, password: str
) -> None:
    deadline = time.monotonic() + ACTION_TIMEOUT_SECONDS
    while time.monotonic() < deadline:
        current = await snapshot(session, page)
        require_amex_url(current)
        if is_dashboard_ready(current):
            return
        if 'textbox "User ID"' in current:
            await authenticate(session, page, user, password, current)
            return
        await asyncio.sleep(0.5)
    raise RuntimeError("Timed out waiting for Amex dashboard or login page")


async def navigate_to_activity(session: BrowserOSSession, page: int) -> str:
    statements = await navigate_to_statements(session, page)
    deadline = time.monotonic() + ACTION_TIMEOUT_SECONDS
    while time.monotonic() < deadline:
        try:
            activity_ref = ref_for_names(
                statements,
                "link",
                ("Go to Statement Activity", "View Statement Activity"),
            )
            break
        except RuntimeError:
            await asyncio.sleep(0.5)
            statements = await snapshot(session, page)
    else:
        raise RuntimeError("Timed out waiting for Amex statement activity link")
    await click(session, page, activity_ref)
    deadline = time.monotonic() + ACTION_TIMEOUT_SECONDS
    while time.monotonic() < deadline:
        activity = await snapshot(session, page)
        if is_activity_page(activity):
            return activity
        await asyncio.sleep(0.5)
    raise RuntimeError("Timed out waiting for Amex statement activity page")


async def navigate_to_statements(session: BrowserOSSession, page: int) -> str:
    deadline = time.monotonic() + ACTION_TIMEOUT_SECONDS
    while time.monotonic() < deadline:
        current = await snapshot(session, page)
        require_amex_url(current)
        if urlparse(amex_url(current)).path.rstrip("/") == "/activity/statements":
            return current
        try:
            statement_ref = ref_for_roles(current, "Statement", ("link", "button"))
            break
        except RuntimeError:
            await asyncio.sleep(0.5)
    else:
        raise RuntimeError("Timed out waiting for Amex Statement control")
    await click(session, page, statement_ref)
    statements = await wait_for_snapshot(
        session, page, "Export Statement Data", ACTION_TIMEOUT_SECONDS
    )
    await click(session, page, ref_for(statements, "link", "Export Statement Data"))
    deadline = time.monotonic() + ACTION_TIMEOUT_SECONDS
    while time.monotonic() < deadline:
        statement_page = await snapshot(session, page)
        if (
            urlparse(amex_url(statement_page)).path.rstrip("/")
            == "/activity/statements"
        ):
            return statement_page
        await asyncio.sleep(0.5)
    raise RuntimeError("Timed out waiting for Amex statements page")


async def export_latest_csv(
    session: BrowserOSSession, page: int, activity_snapshot: str
) -> Path:
    return await export_csv(session, page, activity_snapshot)


def statement_ref(snapshot: str, month: str) -> str:
    label = datetime.strptime(month, "%Y-%m").strftime("%B %Y")
    match = re.search(
        rf'- button "Download \d{{1,2}} {re.escape(label)} Statement"[^\n]*\[ref=(e\d+)\]',
        snapshot,
    )
    if match is None:
        raise RuntimeError(f"Amex did not offer a statement for {label}")
    return match.group(1)


async def statement_snapshot_for_month(
    session: BrowserOSSession, page: int, month: str
) -> tuple[str, str]:
    deadline = time.monotonic() + ACTION_TIMEOUT_SECONDS
    while time.monotonic() < deadline:
        current = await snapshot(session, page)
        try:
            return current, statement_ref(current, month)
        except RuntimeError:
            await asyncio.sleep(0.5)
    label = datetime.strptime(month, "%Y-%m").strftime("%B %Y")
    raise RuntimeError(f"Timed out waiting for Amex statement for {label}")


async def export_csv(
    session: BrowserOSSession,
    page: int,
    source_snapshot: str,
    *,
    ref: str | None = None,
) -> Path:
    if ref is None:
        deadline = time.monotonic() + ACTION_TIMEOUT_SECONDS
        while time.monotonic() < deadline:
            try:
                ref = ref_for_roles(source_snapshot, "Download", ("button", "link"))
                break
            except RuntimeError:
                await asyncio.sleep(0.5)
                source_snapshot = await snapshot(session, page)
        else:
            raise RuntimeError("Timed out waiting for Amex Download control")
    await click(
        session,
        page,
        ref,
    )
    dialog = await wait_for_snapshot(session, page, "CSV", ACTION_TIMEOUT_SECONDS)
    csv_ref = ref_for(dialog, "radio", "CSV")
    await session.call("act", {"page": page, "kind": "check", "ref": csv_ref})
    dialog = await snapshot(session, page)
    return download_path(
        await session.call(
            "download", {"page": page, "ref": ref_for(dialog, "link", "Download")}
        )
    )


async def run_download(
    output_dir: str,
    statement_months: list[str] | None = None,
    *,
    session_factory: BrowserOSSessionFactory = FastMCPBrowserOSSession,
) -> dict[str, Any]:
    user, password = credentials()
    endpoint = browseros_mcp_url()
    async with session_factory(endpoint) as session:
        page = page_from_response(
            await session.call("tabs", {"action": "new", "url": AUTHENTICATED_URL})
        )
        await authenticate_if_needed(session, page, user, password)
        requested = statement_months or ["latest"]
        downloads: list[tuple[str, Path]] = []
        statements_ready = False
        activity: str | None = None
        for item in requested:
            if item == "latest":
                if activity is None:
                    activity = await navigate_to_activity(session, page)
                downloads.append(
                    (
                        "amex-latest.csv",
                        await export_latest_csv(session, page, activity),
                    )
                )
                continue
            if not statements_ready:
                await navigate_to_statements(session, page)
                statements_ready = True
            statements, download_ref = await statement_snapshot_for_month(
                session, page, item
            )
            downloads.append(
                (
                    f"amex-{item}.csv",
                    await export_csv(session, page, statements, ref=download_ref),
                )
            )
    destination_dir = Path(output_dir).expanduser().resolve()
    destinations = [destination_dir / name for name, _ in downloads]
    collisions = [str(path) for path in destinations if path.exists()]
    if collisions:
        raise FileExistsError(f"Refusing to overwrite existing file: {collisions[0]}")
    for _, downloaded in downloads:
        validate_download(downloaded)
    saved: list[Path] = []
    try:
        for name, downloaded in downloads:
            saved.append(move_download(downloaded, destination_dir, name))
    except Exception:
        for path in saved:
            path.unlink(missing_ok=True)
        raise
    return {"status": "downloaded", "paths": [str(path) for path in saved]}


@mcp.tool
async def download_amex_activity(
    output_dir: str, statement_months: list[str] | None = None
) -> dict[str, Any]:
    """Download latest and selected Amex statement CSVs through BrowserOS neo."""
    return await run_download(output_dir, statement_months)


if __name__ == "__main__":
    mcp.run()

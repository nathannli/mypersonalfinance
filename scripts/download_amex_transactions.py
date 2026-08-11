#!/usr/bin/env python3
"""Download Amex Canada activity through Browserbase or BrowserOS neo."""

import argparse
import asyncio
import json
import logging
import os
import re
import shlex
import shutil
import subprocess
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlparse
from zipfile import ZipFile

import messages
from dotenv import load_dotenv

load_dotenv()

LOGIN_URL = "https://www.americanexpress.com/en-ca/account/login/"
AUTHENTICATED_URL = "https://global.americanexpress.com/"
AUTH_TIMEOUT_SECONDS = 5 * 60
ACTION_TIMEOUT_SECONDS = 30
REMOTE_OPEN_TIMEOUT_SECONDS = 90
MFA_CODE_TIMEOUT_SECONDS = 2 * 60
MFA_CODE_INITIAL_DELAY_SECONDS = 10
BROWSE_SESSION = "mypersonalfinance-amex"
DRIVER_DAEMON_TIMEOUT = (
    f'Timed out waiting for driver daemon session "{BROWSE_SESSION}".'
)
NO_ACTIVE_PAGE_ERROR = f'No active page in session "{BROWSE_SESSION}".'
MFA_CODE_PATTERN = re.compile(r"\b(\d{6})\b")
AMEX_SMS_MARKERS = ("american express", "amex")
AMEX_BROWSER_BACKENDS = ("browserbase", "browseros")

logger = logging.getLogger("download_amex_transactions")


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Download Amex Canada statement activity without loading it"
    )
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument(
        "--database", choices=("finance", "parents_finance"), required=True
    )
    parser.add_argument(
        "--months",
        metavar="YYYY-MM|latest",
        nargs="+",
        help="Download statement months and/or the latest activity",
    )
    args = parser.parse_args(argv)
    if args.months:
        for month in args.months:
            if month == "latest":
                continue
            try:
                datetime.strptime(month, "%Y-%m")
            except ValueError:
                parser.error(
                    f"Invalid statement month {month!r}; use YYYY-MM or latest"
                )
        if len(set(args.months)) != len(args.months):
            parser.error("Statement months must not be repeated")
    return args


def amex_browser_backend() -> str:
    backend = os.environ.get("AMEX_BROWSER_BACKEND", "browserbase").lower()
    if backend not in AMEX_BROWSER_BACKENDS:
        supported = ", ".join(AMEX_BROWSER_BACKENDS)
        raise RuntimeError(
            f"AMEX_BROWSER_BACKEND must be one of: {supported} (got {backend!r})"
        )
    return backend


def run_browse(
    args: list[str],
    *,
    timeout: int = ACTION_TIMEOUT_SECONDS,
    session: str = BROWSE_SESSION,
    remote: bool = True,
    retry_on_no_active_page: bool = True,
) -> str:
    if remote and not os.environ.get("BROWSERBASE_API_KEY"):
        raise RuntimeError(
            "BROWSERBASE_API_KEY is required; export it before starting Browserbase"
        )
    command = ["browse", *args]
    if remote:
        command.extend(["--remote", "--session", session])
    try:
        result = subprocess.run(
            command,
            text=True,
            capture_output=True,
            check=False,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired as exc:
        raise RuntimeError(f"Browserbase command timed out: {' '.join(args)}") from exc
    if result.returncode:
        detail = result.stderr.strip() or result.stdout.strip()
        if (
            remote
            and retry_on_no_active_page
            and args[0] != "open"
            and NO_ACTIVE_PAGE_ERROR in detail
        ):
            logger.info("Browserbase lost active page. Reopening Amex dashboard...")
            run_browse(
                ["open", AUTHENTICATED_URL],
                timeout=REMOTE_OPEN_TIMEOUT_SECONDS,
                session=session,
                retry_on_no_active_page=False,
            )
            return run_browse(
                args,
                timeout=timeout,
                session=session,
                remote=remote,
                retry_on_no_active_page=False,
            )
        raise RuntimeError(f"Browserbase command failed: {detail}")
    return result.stdout


def browse_json(output: str) -> dict:
    match = re.search(r"(?m)^\{.*\}\s*$", output, re.DOTALL)
    if not match:
        raise RuntimeError("Browserbase returned an unreadable response")
    return json.loads(match.group(0))


def browse_open() -> tuple[str, str]:
    try:
        response = browse_json(
            run_browse(["open", LOGIN_URL], timeout=REMOTE_OPEN_TIMEOUT_SECONDS)
        )
    except RuntimeError as exc:
        if DRIVER_DAEMON_TIMEOUT not in str(exc):
            raise
        logger.info("Stale browser session found. Starting a fresh session...")
        run_browse(["stop", "--force", "--session", BROWSE_SESSION], remote=False)
        response = browse_json(
            run_browse(["open", LOGIN_URL], timeout=REMOTE_OPEN_TIMEOUT_SECONDS)
        )
    session_id = response.get("browserbaseSessionId")
    session_url = response.get("browserbaseSessionUrl")
    if not session_id or not session_url:
        raise RuntimeError("Browserbase did not return a session or live-view URL")
    return session_id, session_url


def cleanup_browserbase_session() -> None:
    try:
        run_browse(["stop", "--force", "--session", BROWSE_SESSION], remote=False)
    except RuntimeError as exc:
        logger.warning("Browserbase session cleanup failed: %s", exc)


def browse_url() -> str:
    url = browse_json(run_browse(["get", "url"])).get("url")
    if not url:
        raise RuntimeError("Browserbase did not return an active page URL")
    return url


def browse_click_xpath(xpath: str) -> None:
    run_browse(["click", xpath])


def browse_eval(expression: str) -> str:
    result = browse_json(run_browse(["eval", expression])).get("result")
    if result is None:
        raise RuntimeError("Browserbase did not return an evaluation result")
    return str(result)


def browse_fill(selector: str, value: str, field_name: str) -> None:
    if not value:
        raise RuntimeError(f"{field_name} is missing from environment configuration")
    run_browse(["fill", selector, value])


def fill_browserbase_credentials() -> None:
    browse_fill(
        "#eliloUserID",
        os.environ.get("AMEX_USER", ""),
        "AMEX_USER",
    )
    browse_fill(
        '//input[@type="password"][1]',
        os.environ.get("AMEX_PASSWORD", ""),
        "AMEX_PASSWORD",
    )


def select_sms_mfa_browserbase() -> datetime:
    logger.info("Waiting for MFA delivery options...")
    wait_until(
        lambda: (
            "Select how you'd like to receive your code"
            in browse_eval("document.body.innerText")
        ),
        "Amex MFA delivery selection",
    )
    browse_click_xpath('//form//input[@type="radio"][1]')
    requested_at = datetime.now(timezone.utc).replace(tzinfo=None)
    logger.info("Selected SMS for MFA. Requesting verification code...")
    browse_click_xpath('//button[normalize-space(.)="Continue"]')
    return requested_at


def wait_for_amex_sms_code(requested_at: datetime) -> str:
    try:
        database = messages.get_db()
    except PermissionError as exc:
        raise RuntimeError(
            "macos-messages needs Full Disk Access for this terminal application"
        ) from exc

    sms_chat_ids = [chat.id for chat in database.chats(service="SMS", limit=100)]
    if not sms_chat_ids:
        raise RuntimeError("macos-messages found no SMS conversations")

    logger.info("Waiting 10 seconds before checking Messages for MFA code...")
    time.sleep(MFA_CODE_INITIAL_DELAY_SECONDS)
    logger.info("Waiting for recent Amex MFA code in Messages...")
    deadline = time.monotonic() + MFA_CODE_TIMEOUT_SECONDS
    while time.monotonic() < deadline:
        for message in reversed(
            list(
                database.messages(
                    chat_ids=sms_chat_ids,
                    after=requested_at,
                    limit=25,
                    include_unsent=False,
                    include_unknown_senders=True,
                )
            )
        ):
            text = message.text or ""
            if message.is_from_me or not any(
                marker in text.lower() for marker in AMEX_SMS_MARKERS
            ):
                continue
            if code := MFA_CODE_PATTERN.search(text):
                logger.info("Received recent Amex MFA code.")
                return code.group(1)
        time.sleep(0.5)
    raise RuntimeError("Timed out waiting for a recent Amex SMS verification code")


def submit_sms_mfa_browserbase(requested_at: datetime) -> None:
    logger.info("Waiting for Amex MFA code entry...")
    wait_until(
        lambda: "Please enter the verification code"
        in browse_eval("document.body.innerText"),
        "Amex MFA code entry",
    )
    browse_fill(
        '//form//input[@type="text"][1]',
        wait_for_amex_sms_code(requested_at),
        "Amex MFA code",
    )
    logger.info("Submitting Amex MFA code...")
    browse_click_xpath('//button[normalize-space(.)="Continue"]')


def complete_trust_device_browserbase() -> None:
    logger.info("Checking Trust Device verification...")

    def trust_device_or_authenticated() -> str | None:
        if browse_url().startswith(AUTHENTICATED_URL):
            return "authenticated"
        if "Security Verification: Trust Device" in browse_eval(
            "document.body.innerText"
        ):
            return "trust-device"
        return None

    state = wait_until(
        trust_device_or_authenticated,
        "Amex trust-device security verification",
    )
    if state == "trust-device":
        logger.info(
            "Trust Device verification shown. Continuing without trusting device..."
        )
        browse_click_xpath('//button[normalize-space(.)="Continue"]')
    else:
        logger.info("Trust Device verification not required.")


def wait_for_browserbase_url(path: str, message: str) -> str:
    def matching_url() -> str | None:
        dismiss_browserbase_popups()
        current = browse_url()
        return current if urlparse(current).path == path else None

    return wait_until(
        matching_url,
        message,
    )


def dismiss_browserbase_popups() -> None:
    for _ in range(3):
        title = browse_eval(
            """
(() => {
  const labels = new Set(['close', 'got it', 'dismiss', 'continue', 'explore on my own']);
  for (const dialog of document.querySelectorAll('[role="dialog"], [aria-modal="true"]')) {
    if (dialog.offsetParent === null) continue;
    const button = [...dialog.querySelectorAll('button')].find(button => {
      const text = button.textContent.trim().toLowerCase();
      const ariaLabel = (button.getAttribute('aria-label') || '').toLowerCase();
      return labels.has(text) || ariaLabel.includes('close');
    });
    if (!button) continue;
    const title = (dialog.querySelector('h1, h2, h3, h4, [role="heading"]')?.textContent || dialog.textContent)
      .trim()
      .split('\\n')[0];
    button.click();
    return title;
  }
  return '';
})()
"""
        )
        if not title:
            return
        logger.info("Dismissed Amex popup: %s", title)


def wait_for_statement_activity_browserbase() -> None:
    wait_until(
        lambda: (
            dismiss_browserbase_popups(),
            urlparse(browse_url()).path == "/activity"
            and "Download" in browse_eval("document.body.innerText"),
        )[1],
        "Amex statement activity page in Browserbase",
    )


def browserbase_download(
    session_id: str,
    output_dir: Path,
    *,
    known_downloads: set[str] | None = None,
    destination_name: str | None = None,
    return_download_names: bool = False,
) -> Path | tuple[Path, set[str]]:
    output_dir = output_dir.expanduser().resolve()
    output_dir.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="amex-browserbase-") as temporary:
        archive = Path(temporary) / "downloads.zip"
        run_browse(
            [
                "cloud",
                "sessions",
                "downloads",
                "get",
                session_id,
                "--output",
                str(archive),
            ],
            timeout=ACTION_TIMEOUT_SECONDS,
            remote=False,
        )
        if not archive.exists():
            raise RuntimeError("Browserbase returned no download archive")
        with ZipFile(archive) as zip_file:
            csv_names = [
                name
                for name in zip_file.namelist()
                if name.lower().endswith(".csv") and not name.endswith("/")
            ]
            new_csv_names = (
                set(csv_names)
                if known_downloads is None
                else set(csv_names) - known_downloads
            )
            if len(new_csv_names) != 1:
                raise RuntimeError(
                    f"Expected exactly one new CSV in Browserbase downloads; found {len(new_csv_names)}"
                )
            csv_name = new_csv_names.pop()
            downloaded = Path(temporary) / sanitize_filename(Path(csv_name).name)
            downloaded.write_bytes(zip_file.read(csv_name))
            saved = move_download(downloaded, output_dir, destination_name)
            if return_download_names:
                return saved, set(csv_names)
            return saved


def download_browser(
    session_id: str,
    output_dir: Path,
    *,
    known_downloads: set[str] | None = None,
    destination_name: str | None = None,
    return_download_names: bool = False,
) -> Path | tuple[Path, set[str]]:
    if (
        known_downloads is None
        and destination_name is None
        and not return_download_names
    ):
        return browserbase_download(session_id, output_dir)
    return browserbase_download(
        session_id,
        output_dir,
        known_downloads=known_downloads,
        destination_name=destination_name,
        return_download_names=return_download_names,
    )


def run_osascript(script: str, operation: str = "control Chrome") -> str:
    try:
        result = subprocess.run(
            ["osascript"],
            input=script,
            text=True,
            capture_output=True,
            check=False,
            timeout=5,
        )
    except subprocess.TimeoutExpired as exc:
        raise RuntimeError(
            f"Chrome AppleScript timed out while trying to {operation}"
        ) from exc
    if result.returncode:
        detail = result.stderr.strip() or result.stdout.strip()
        raise RuntimeError(f"Chrome AppleScript failed: {detail}")
    return result.stdout.strip()


def find_amex_url() -> str | None:
    result = run_osascript(
        """
tell application "Google Chrome"
  set fallbackUrl to ""
  repeat with windowIndex from 1 to count of windows
    repeat with tabIndex from 1 to count of tabs of window windowIndex
      set tabUrl to URL of tab tabIndex of window windowIndex
      if tabUrl starts with "https://global.americanexpress.com/" then return tabUrl
      if fallbackUrl is "" and tabUrl contains "americanexpress.com" then set fallbackUrl to tabUrl
    end repeat
  end repeat
  return fallbackUrl
end tell
""",
        "inspect Chrome tabs",
    )
    return result or None


def execute_chrome_js(javascript: str) -> str:
    quoted_javascript = json.dumps(javascript)
    return run_osascript(
        f"""
tell application "Google Chrome"
  repeat with windowIndex from 1 to count of windows
    repeat with tabIndex from 1 to count of tabs of window windowIndex
      set tabUrl to URL of tab tabIndex of window windowIndex
      if tabUrl starts with "{AUTHENTICATED_URL}" then
        set targetWindow to window windowIndex
        set targetTab to tab tabIndex of targetWindow
        set active tab index of targetWindow to tabIndex
        set index of targetWindow to 1
        activate
        return execute targetTab javascript {quoted_javascript}
      end if
    end repeat
  end repeat
end tell
error "No authenticated Amex tab found"
""",
        "control the authenticated Amex tab",
    )


def wait_until(predicate, message: str, timeout: int = ACTION_TIMEOUT_SECONDS):
    deadline = time.monotonic() + timeout
    last_error = None
    while time.monotonic() < deadline:
        try:
            result = predicate()
            if result:
                return result
        except RuntimeError as exc:
            last_error = exc
        time.sleep(0.5)
    suffix = f": {last_error}" if last_error else ""
    raise RuntimeError(f"Timed out waiting for {message}{suffix}")


def wait_for_authentication() -> None:
    current_url = find_amex_url()
    if current_url and current_url.startswith(AUTHENTICATED_URL):
        return
    subprocess.run(["open", "-a", "Google Chrome", LOGIN_URL], check=True, timeout=10)
    logger.info("Complete Amex login, MFA, and any security challenge in Chrome.")
    wait_until(
        lambda: (url := find_amex_url()) and url.startswith(AUTHENTICATED_URL),
        "an authenticated Amex account landing page",
        AUTH_TIMEOUT_SECONDS,
    )


def wait_for_browserbase_authentication() -> str:
    session_id, session_url = browse_open()
    logger.info("Browserbase live session: %s", session_url)
    logger.info("Attempting Amex login...")
    fill_browserbase_credentials()
    logger.info("Credentials filled. Submitting login; complete CAPTCHA if prompted.")
    browse_click_xpath('//button[normalize-space(.)="Log In"]')
    submit_sms_mfa_browserbase(select_sms_mfa_browserbase())
    complete_trust_device_browserbase()

    def authenticated_url() -> str | None:
        current = browse_url()
        return current if current.startswith(AUTHENTICATED_URL) else None

    wait_until(
        authenticated_url,
        "an authenticated Amex account landing page in Browserbase",
        AUTH_TIMEOUT_SECONDS,
    )
    logger.info("Logged in. Got to main page.")
    return session_id


def click_visible(text: str) -> None:
    encoded_text = json.dumps(text)
    result = execute_chrome_js(
        f"""
(() => {{
  const expected = {encoded_text};
  const target = [...document.querySelectorAll('a,button,[role=menuitem],[role=tab]')]
    .find(element =>
      (element.innerText || element.getAttribute('aria-label') || '').trim() === expected
      && element.offsetParent !== null
    );
  if (!target) return 'missing';
  target.click();
  return 'clicked';
}})()
"""
    )
    if result != "clicked":
        raise RuntimeError(f"Amex page changed: could not find visible {text!r}")


def navigate_visible_link(text: str) -> None:
    encoded_text = json.dumps(text)
    result = execute_chrome_js(
        f"""
(() => {{
  const expected = {encoded_text};
  const target = [...document.querySelectorAll('a[href]')]
    .find(element => element.textContent.trim() === expected && element.offsetParent !== null);
  if (!target) return 'missing';
  location.assign(target.href);
  return 'navigating';
}})()
"""
    )
    if result != "navigating":
        raise RuntimeError(f"Amex page changed: could not find visible link {text!r}")


def ensure_single_card() -> None:
    count = execute_chrome_js(
        """
(() => {
  const accountKeys = new Set();
  for (const link of document.querySelectorAll('a[href]')) {
    try {
      const key = new URL(link.href).searchParams.get('account_key');
      if (key) accountKeys.add(key);
    } catch (_) {}
  }
  return String(accountKeys.size);
})()
"""
    )
    if count != "1":
        raise RuntimeError(f"Expected one Amex card; found {count}")


def navigate_to_export() -> None:
    pathname = execute_chrome_js("location.pathname")
    ensure_single_card()
    if pathname == "/activity":
        return
    click_visible("Statement")
    wait_until(
        lambda: (click_visible("Export Statement Data"), True)[1],
        "Export Statement Data control",
    )
    wait_until(
        lambda: execute_chrome_js("location.pathname") == "/activity/statements",
        "Amex statements page",
    )
    wait_until(
        lambda: (navigate_visible_link("Go to Statement Activity"), True)[1],
        "Go to Statement Activity control",
    )
    wait_until(
        lambda: execute_chrome_js("location.pathname") == "/activity",
        "Amex statement activity page",
    )


def navigate_to_export_browserbase(statement_months: list[str] | None = None) -> None:
    url = browse_url()
    dismiss_browserbase_popups()
    ensure_single_card_browserbase()
    if urlparse(url).path == "/activity":
        logger.info("Already at statement activity page.")
        return
    logger.info("Navigating to statements page...")
    dismiss_browserbase_popups()
    browse_click_xpath('//*[self::a or self::button][normalize-space(.)="Statement"]')
    logger.info("Opening statement export...")
    dismiss_browserbase_popups()
    browse_click_xpath('//a[normalize-space(.)="Export Statement Data"]')
    wait_for_browserbase_url(
        "/activity/statements", "Amex statements page in Browserbase"
    )
    logger.info("Got to statements page.")
    if statement_months:
        return
    navigate_to_latest_activity_browserbase()


def navigate_to_latest_activity_browserbase() -> None:
    logger.info("Navigating to statement activity page...")
    dismiss_browserbase_popups()
    browse_click_xpath('//a[normalize-space(.)="Go to Statement Activity"]')
    wait_for_statement_activity_browserbase()
    logger.info("Got to statement activity page.")
    try:
        browse_click_xpath('//button[normalize-space(.)="Explore On My Own"]')
    except RuntimeError:
        pass


def ensure_single_card_browserbase() -> None:
    count = (
        browse_eval(
            "String(new Set([...document.querySelectorAll('a[href]')].map(a => "
            "{try{return new URL(a.href).searchParams.get('account_key')}catch(_){return null}}).filter(Boolean)).size)"
        )
        .splitlines()[-1]
        .strip()
    )
    if count != "1":
        raise RuntimeError(f"Expected one Amex card; found {count}")


def select_csv_and_get_download_point() -> tuple[int, int]:
    csv_available = execute_chrome_js(
        "String(Boolean(document.querySelector('input[type=radio][value=csv]')))"
    )
    if csv_available != "true":
        wait_until(
            lambda: (click_visible("Download"), True)[1],
            "Download control",
        )
    wait_until(
        lambda: execute_chrome_js(
            "String(Boolean(document.querySelector('input[type=radio][value=csv]')))"
        )
        == "true",
        "Amex export dialog",
    )
    coordinates = execute_chrome_js(
        """
(() => {
  const csv = document.querySelector('input[type=radio][value=csv]');
  csv.click();
  const dialog = csv.closest('[role=dialog]');
  const link = dialog && [...dialog.querySelectorAll('a')].find(element =>
    element.textContent.trim() === 'Download' && element.offsetParent !== null
  );
  if (!link || !csv.checked) return '';
  const rect = link.getBoundingClientRect();
  return JSON.stringify({
    x: Math.round(screenX + rect.x + rect.width / 2),
    y: Math.round(screenY + outerHeight - innerHeight + rect.y + rect.height / 2)
  });
})()
"""
    )
    if not coordinates:
        raise RuntimeError("Amex page changed: CSV download link is unavailable")
    point = json.loads(coordinates)
    return point["x"], point["y"]


def select_csv_browserbase() -> None:
    dismiss_browserbase_popups()
    browse_click_xpath('//*[self::a or self::button][normalize-space(.)="Download"]')
    wait_until(
        lambda: (
            browse_eval(
                "String(Boolean(document.querySelector('input[type=radio][value=csv]')))"
            )
            .splitlines()[-1]
            .strip()
            == "true"
        ),
        "Amex CSV export dialog in Browserbase",
    )
    browse_click_xpath('input[type="radio"][value="csv"]')
    browse_click_xpath('//a[normalize-space(.)="Download"]')


def select_statement_month_csv_browserbase(month: str) -> None:
    label = datetime.strptime(month, "%Y-%m").strftime("%B %Y")
    logger.info("Opening statement export for %s...", label)
    selected = browse_eval(
        f"""
(() => {{
  const label = {label!r};
  for (const date of document.querySelectorAll('[data-testid$="/date"]')) {{
    if (date.offsetParent === null || !date.textContent.includes(label)) continue;
    const button = date.parentElement?.querySelector('button[title="Download"]');
    if (!button) continue;
    button.click();
    return date.textContent.trim();
  }}
  return '';
}})()
"""
    )
    if not selected:
        raise RuntimeError(f"Amex did not offer a statement for {label}")
    logger.info("Downloading statement with closing date %s...", selected)
    wait_until(
        lambda: browse_eval(
            "String(Boolean(document.querySelector('input[type=radio][value=csv]')))"
        )
        == "true",
        f"Amex CSV export dialog for {label} in Browserbase",
    )
    browse_click_xpath('input[type="radio"][value="csv"]')
    browse_click_xpath('//a[normalize-space(.)="Download"]')


def native_click(x: int, y: int) -> None:
    swift = (
        "import CoreGraphics; import Foundation; "
        f"let p = CGPoint(x: {x}, y: {y}); "
        "CGEvent(mouseEventSource: nil, mouseType: .mouseMoved, "
        "mouseCursorPosition: p, mouseButton: .left)?.post(tap: .cghidEventTap); "
        "usleep(100000); "
        "CGEvent(mouseEventSource: nil, mouseType: .leftMouseDown, "
        "mouseCursorPosition: p, mouseButton: .left)?.post(tap: .cghidEventTap); "
        "usleep(50000); "
        "CGEvent(mouseEventSource: nil, mouseType: .leftMouseUp, "
        "mouseCursorPosition: p, mouseButton: .left)?.post(tap: .cghidEventTap)"
    )
    result = subprocess.run(
        ["swift", "-e", swift],
        text=True,
        capture_output=True,
        check=False,
        timeout=10,
    )
    if result.returncode:
        raise RuntimeError(
            "Native Chrome click failed. Grant Accessibility permission to this "
            f"terminal application: {result.stderr.strip()}"
        )


def chrome_download_dir() -> Path:
    chrome_root = Path.home() / "Library" / "Application Support" / "Google" / "Chrome"
    local_state = json.loads((chrome_root / "Local State").read_text())
    profile = local_state.get("profile", {}).get("last_used", "Default")
    preferences = json.loads((chrome_root / profile / "Preferences").read_text())
    download = preferences.get("download", {})
    if download.get("prompt_for_download"):
        raise RuntimeError("Disable Chrome's 'Ask where to save each file' setting")
    return Path(download.get("default_directory") or Path.home() / "Downloads")


def sanitize_filename(suggested_filename: str) -> str:
    filename = Path(suggested_filename).name
    filename = re.sub(r"[^A-Za-z0-9._-]+", "_", filename).strip("._")
    if not filename:
        raise ValueError("Amex supplied an unusable download filename")
    return filename


def wait_for_download(download_dir: Path, before: set[Path]) -> Path:
    deadline = time.monotonic() + ACTION_TIMEOUT_SECONDS
    while time.monotonic() < deadline:
        candidates = {
            path
            for path in download_dir.glob("*.csv")
            if path not in before and path.is_file()
        }
        if len(candidates) == 1:
            return candidates.pop()
        if len(candidates) > 1:
            raise RuntimeError("Multiple new CSV downloads found; cannot choose safely")
        time.sleep(0.5)
    for partial in download_dir.glob("*.crdownload"):
        if partial not in before:
            partial.unlink(missing_ok=True)
    raise RuntimeError("Timed out waiting for Chrome to finish the Amex CSV download")


def move_download(
    download: Path, output_dir: Path, destination_name: str | None = None
) -> Path:
    output_dir = output_dir.expanduser().resolve()
    output_dir.mkdir(parents=True, exist_ok=True)
    destination = output_dir / sanitize_filename(destination_name or download.name)
    try:
        with destination.open("xb") as reserved, download.open("rb") as source:
            shutil.copyfileobj(source, reserved)
    except FileExistsError:
        raise FileExistsError(f"Refusing to overwrite existing file: {destination}")
    except OSError:
        destination.unlink(missing_ok=True)
        raise
    download.unlink()
    return destination


def validate_download(path: Path) -> None:
    from sources.csv.amex import AmexStatement

    columns = AmexStatement(str(path)).get_df().columns
    expected = ["date", "merchant", "cost", "cc_category"]
    if columns != expected:
        raise RuntimeError(
            f"Downloaded Amex file has unsupported standardized columns: {columns}"
        )


def loader_command(path: Path, database: str) -> str:
    return shlex.join(
        [
            "uv",
            "run",
            "python",
            "load-transactions.py",
            "--type",
            "amex",
            "--filepath",
            str(path),
            "--database",
            database,
        ]
    )


def run(
    output_dir: Path,
    database: str,
    statement_months: list[str] | None = None,
) -> Path | list[Path]:
    if amex_browser_backend() == "browseros":
        from scripts.amex_browseros_bridge import run_download

        result = asyncio.run(run_download(str(output_dir), statement_months))
        saved_paths = [Path(path) for path in result["paths"]]
        for saved_path in saved_paths:
            logger.info("Downloaded Amex statement: %s", saved_path)
            logger.info("Load manually: %s", loader_command(saved_path, database))
        return saved_paths if statement_months else saved_paths[0]
    logger.info(
        "Starting Browserbase authentication session...",
    )
    try:
        session_id = wait_for_browserbase_authentication()
        requested = statement_months or []
        historic_months = [month for month in requested if month != "latest"]
        download_latest = not requested or "latest" in requested
        navigate_to_export_browserbase(historic_months or None)
        saved_paths: list[Path] = []
        known_downloads: set[str] = set()
        if historic_months:
            for month in historic_months:
                select_statement_month_csv_browserbase(month)
                saved, known_downloads = download_browser(
                    session_id,
                    output_dir,
                    known_downloads=known_downloads,
                    destination_name=f"amex-{month}.csv",
                    return_download_names=True,
                )
                validate_download(saved)
                logger.info("Downloaded Amex statement for %s: %s", month, saved)
                logger.info("Load manually: %s", loader_command(saved, database))
                saved_paths.append(saved)
        if download_latest:
            if historic_months:
                navigate_to_latest_activity_browserbase()
            logger.info("Selecting CSV export...")
            select_csv_browserbase()
            logger.info("Downloading latest Amex statement activity...")
            if historic_months:
                saved_path, known_downloads = download_browser(
                    session_id,
                    output_dir,
                    known_downloads=known_downloads,
                    return_download_names=True,
                )
            else:
                saved_path = download_browser(session_id, output_dir)
            validate_download(saved_path)
            logger.info("Downloaded Amex statement: %s", saved_path)
            logger.info("Load manually: %s", loader_command(saved_path, database))
            saved_paths.append(saved_path)
        if historic_months:
            return saved_paths
        return saved_paths[0]
    finally:
        cleanup_browserbase_session()


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s-%(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
    args = parse_args(argv)
    try:
        run(
            args.output_dir,
            args.database,
            args.months,
        )
    except KeyboardInterrupt:
        logger.warning("Interrupted.")
        return 130
    except Exception as exc:
        logger.error("ERROR: %s", exc)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

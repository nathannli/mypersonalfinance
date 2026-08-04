#!/usr/bin/env python3
"""Download Amex Canada activity through the user's normal Google Chrome."""

import argparse
import json
import re
import shlex
import shutil
import subprocess
import sys
import time
from pathlib import Path

LOGIN_URL = "https://www.americanexpress.com/en-ca/account/login/"
AUTHENTICATED_URL = "https://global.americanexpress.com/"
AUTH_TIMEOUT_SECONDS = 5 * 60
ACTION_TIMEOUT_SECONDS = 30


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Download Amex Canada statement activity without loading it"
    )
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument(
        "--database", choices=("finance", "parents_finance"), required=True
    )
    return parser.parse_args(argv)


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
    print("Complete Amex login, MFA, and any security challenge in Chrome.")
    wait_until(
        lambda: (url := find_amex_url()) and url.startswith(AUTHENTICATED_URL),
        "an authenticated Amex account landing page",
        AUTH_TIMEOUT_SECONDS,
    )


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
    if pathname == "/activity":
        return
    ensure_single_card()
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
  const link = [...document.querySelectorAll('a')].find(element =>
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


def move_download(download: Path, output_dir: Path) -> Path:
    output_dir = output_dir.expanduser().resolve()
    output_dir.mkdir(parents=True, exist_ok=True)
    destination = output_dir / sanitize_filename(download.name)
    if destination.exists():
        raise FileExistsError(f"Refusing to overwrite existing file: {destination}")
    return Path(shutil.move(str(download), destination))


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


def run(output_dir: Path, database: str) -> Path:
    print("Checking normal Chrome authentication...", flush=True)
    wait_for_authentication()
    print("Navigating to Amex statement activity...", flush=True)
    navigate_to_export()
    download_dir = chrome_download_dir()
    before = set(download_dir.iterdir())
    print("Selecting CSV export...", flush=True)
    x, y = select_csv_and_get_download_point()
    print("Clicking Chrome download control...", flush=True)
    native_click(x, y)
    print("Waiting for Chrome download...", flush=True)
    saved_path = move_download(wait_for_download(download_dir, before), output_dir)
    validate_download(saved_path)
    print(f"Downloaded Amex statement: {saved_path}")
    print(f"Load manually: {loader_command(saved_path, database)}")
    return saved_path


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    try:
        run(args.output_dir, args.database)
    except Exception as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

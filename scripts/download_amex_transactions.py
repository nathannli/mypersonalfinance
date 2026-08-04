#!/usr/bin/env python3
"""Download Amex Canada statement activity with user-supervised authentication."""

import argparse
import re
import shlex
import sys
from pathlib import Path
from typing import Any

LOGIN_URL = "https://www.americanexpress.com/en-ca/account/login/"
AUTHENTICATED_URL = re.compile(r"^https://global\.americanexpress\.com/")
AUTH_TIMEOUT_MS = 5 * 60 * 1000
ACTION_TIMEOUT_MS = 30 * 1000
AMEX_BROWSER_PROFILE_DIR = (
    Path.home() / ".local" / "share" / "mypersonalfinance" / "amex-browser-profile"
)


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Download Amex Canada statement activity without loading it"
    )
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument(
        "--database", choices=("finance", "parents_finance"), required=True
    )
    return parser.parse_args(argv)


def sanitize_filename(suggested_filename: str) -> str:
    filename = Path(suggested_filename).name
    filename = re.sub(r"[^A-Za-z0-9._-]+", "_", filename).strip("._")
    if not filename:
        raise ValueError("Amex supplied an unusable download filename")
    return filename


def destination_paths(output_dir: Path, suggested_filename: str) -> tuple[Path, Path]:
    destination = output_dir.expanduser().resolve() / sanitize_filename(
        suggested_filename
    )
    partial = destination.with_name(f".{destination.name}.partial")
    if destination.exists():
        raise FileExistsError(f"Refusing to overwrite existing file: {destination}")
    if partial.exists():
        raise FileExistsError(f"Remove stale partial download first: {partial}")
    return destination, partial


def wait_for_authentication(page: Any) -> None:
    page.goto(LOGIN_URL, wait_until="domcontentloaded")
    if AUTHENTICATED_URL.match(page.url):
        return
    print("Complete Amex login, MFA, and any security challenge in the browser.")
    try:
        page.wait_for_url(AUTHENTICATED_URL, timeout=AUTH_TIMEOUT_MS)
    except Exception as exc:
        raise RuntimeError(
            "Timed out waiting for an authenticated Amex account landing page"
        ) from exc


def click_named(page: Any, name: str) -> None:
    pattern = re.compile(rf"^{re.escape(name)}$", re.IGNORECASE)
    for role in ("link", "button", "tab"):
        locator = page.get_by_role(role, name=pattern)
        if locator.count():
            locator.first.click(timeout=ACTION_TIMEOUT_MS)
            return
    text = page.get_by_text(name, exact=True)
    if text.count():
        text.first.click(timeout=ACTION_TIMEOUT_MS)
        return
    raise RuntimeError(f"Amex page changed: could not find {name!r}")


def ensure_single_card(page: Any) -> None:
    selector = page.get_by_role(
        "combobox", name=re.compile(r"card|account", re.IGNORECASE)
    )
    if not selector.count():
        return
    if selector.count() != 1:
        raise RuntimeError(f"Expected one Amex card selector; found {selector.count()}")

    selector.first.click(timeout=ACTION_TIMEOUT_MS)
    options = page.get_by_role("option")
    option_count = options.count()
    page.keyboard.press("Escape")
    if option_count != 1:
        raise RuntimeError(f"Expected one Amex card; found {option_count}")


def navigate_to_export(page: Any) -> None:
    ensure_single_card(page)
    click_named(page, "Statement")
    click_named(page, "Export Statement Data")


def select_csv(page: Any) -> None:
    csv_pattern = re.compile(r"\bCSV\b", re.IGNORECASE)
    radio = page.get_by_role("radio", name=csv_pattern)
    if radio.count():
        radio.first.check(timeout=ACTION_TIMEOUT_MS)
        return
    csv_text = page.get_by_text(csv_pattern)
    if csv_text.count():
        csv_text.first.click(timeout=ACTION_TIMEOUT_MS)
        return
    raise RuntimeError("Amex page changed: CSV export format is unavailable")


def capture_download(page: Any, output_dir: Path) -> Path:
    output_dir = output_dir.expanduser().resolve()
    output_dir.mkdir(parents=True, exist_ok=True)

    try:
        with page.expect_download(timeout=ACTION_TIMEOUT_MS) as download_info:
            click_named(page, "Download")
        download = download_info.value
        destination, partial = destination_paths(
            output_dir, download.suggested_filename
        )
        try:
            download.save_as(partial)
            partial.replace(destination)
        except Exception:
            partial.unlink(missing_ok=True)
            raise
        return destination
    except Exception as exc:
        raise RuntimeError(f"Amex statement download failed: {exc}") from exc


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
    try:
        from playwright.sync_api import sync_playwright
    except ModuleNotFoundError as exc:
        raise RuntimeError(
            "Playwright is not installed. Run `uv sync --extra browser` and "
            "`uv run --extra browser playwright install chromium`."
        ) from exc

    with sync_playwright() as playwright:
        context = playwright.chromium.launch_persistent_context(
            AMEX_BROWSER_PROFILE_DIR,
            headless=False,
            accept_downloads=True,
        )
        try:
            page = context.pages[0] if context.pages else context.new_page()
            wait_for_authentication(page)
            navigate_to_export(page)
            select_csv(page)
            saved_path = capture_download(page, output_dir)
            validate_download(saved_path)
        finally:
            context.close()

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

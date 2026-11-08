#!/usr/bin/env python3
"""Download TD chequing or Visa account activity through BrowserOS neo."""

import argparse
import asyncio
import csv
import logging
import os
from datetime import date, datetime
from pathlib import Path

from dotenv import load_dotenv

from scripts.browser_common import loader_command, validate_download

load_dotenv()

ACCOUNTS = {"visa": "td_visa", "debit": "td_debit"}
# Neither TD export has a header row; the first column is the date, in a
# different format for each account (observed 2026-11-06).
DATE_FORMATS = {"td_visa": "%m/%d/%Y", "td_debit": "%Y-%m-%d"}

logger = logging.getLogger("download_td_transactions")


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Download TD account activity without loading it"
    )
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument(
        "--database", choices=("finance", "parents_finance"), required=True
    )
    parser.add_argument("--account", choices=tuple(ACCOUNTS), required=True)
    parser.add_argument(
        "--since",
        type=date.fromisoformat,
        required=True,
        metavar="YYYY-MM-DD",
        help="Oldest transaction date to keep",
    )
    parser.add_argument(
        "--until",
        type=date.fromisoformat,
        default=None,
        metavar="YYYY-MM-DD",
        help="Newest transaction date to keep (default: today)",
    )
    return parser.parse_args(argv)


def credentials() -> tuple[str, str]:
    values = tuple(os.environ.get(name, "") for name in ("TD_USER", "TD_PASSWORD"))
    if not all(values):
        raise RuntimeError("TD_USER and TD_PASSWORD must be configured locally")
    return values


def destination(output_dir: Path, account: str, since: date) -> Path:
    name = f"td-{account}-{since.isoformat()}-{date.today().isoformat()}.csv"
    return output_dir.expanduser().resolve() / name


def combine_exports(
    exports: list[Path], card_type: str, since: date, until: date, target: Path
) -> int:
    """Write the exports' rows dated since..until into one new file.

    The Visa export is one statement period per file, and a period can start
    before `since`, so rows are filtered here for both accounts. Rows are kept
    exactly as TD wrote them, in file order, and an existing target is refused.
    """
    date_format = DATE_FORMATS[card_type]
    kept: list[list[str]] = []
    for export in exports:
        with export.open(newline="", encoding="utf-8-sig") as handle:
            for row in csv.reader(handle):
                if not row:
                    continue
                try:
                    row_date = datetime.strptime(row[0], date_format).date()
                except ValueError as exc:
                    raise RuntimeError(
                        "TD export has a row with an unexpected date format"
                    ) from exc
                if since <= row_date <= until:
                    kept.append(row)
    if not kept:
        raise RuntimeError(f"TD export has no transactions from {since} to {until}")
    target.parent.mkdir(parents=True, exist_ok=True)
    try:
        with target.open("x", newline="", encoding="utf-8") as handle:
            csv.writer(handle, lineterminator="\r\n").writerows(kept)
    except FileExistsError:
        raise FileExistsError(f"Refusing to overwrite existing file: {target}")
    return len(kept)


def discard(exports: list[Path]) -> None:
    """Delete the raw exports and the private folder the bridge put them in."""
    for export in exports:
        export.unlink(missing_ok=True)
    for folder in {export.parent for export in exports}:
        try:
            folder.rmdir()
        except OSError:
            pass


async def run_scrape(
    account: str, since: date, until: date, user: str, password: str
) -> list[Path]:
    """Seam over the TD BrowserOS bridge, which owns the browser session.

    Returns one raw CSV per download: one for debit, one per statement period
    for Visa. They sit in a private temporary folder.
    """
    from scripts.td_browseros_bridge import run_download

    return [
        Path(path) for path in await run_download(account, since, until, user, password)
    ]


def run(
    output_dir: Path,
    database: str,
    account: str,
    since: date,
    until: date | None = None,
) -> Path:
    until = until or date.today()
    if until < since:
        raise ValueError("--until must not be earlier than --since")
    user, password = credentials()
    card_type = ACCOUNTS[account]
    target = destination(output_dir, account, since)
    if target.exists():
        raise FileExistsError(f"Refusing to overwrite existing file: {target}")
    logger.info("Downloading TD %s activity since %s...", account, since)
    exports = asyncio.run(run_scrape(account, since, until, user, password))
    try:
        rows = combine_exports(exports, card_type, since, until, target)
    finally:
        discard(exports)
    try:
        validate_download(target, card_type)
    except Exception:
        target.unlink(missing_ok=True)
        raise
    logger.info("Downloaded TD %s activity: %s (%s rows)", account, target, rows)
    logger.info("Load manually: %s", loader_command(target, database, card_type))
    return target


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s-%(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
    args = parse_args(argv)
    try:
        run(args.output_dir, args.database, args.account, args.since, args.until)
    except KeyboardInterrupt:
        logger.warning("Interrupted.")
        return 130
    except Exception as exc:
        logger.error("ERROR: %s", exc)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

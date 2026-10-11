#!/usr/bin/env python3
"""Download Simplii Visa or debit account activity through BrowserOS neo."""

import argparse
import asyncio
import logging
import os
from datetime import date
from pathlib import Path

from dotenv import load_dotenv

from scripts.browser_common import loader_command, move_download, validate_download

load_dotenv()

ACCOUNTS = {"visa": "simplii_visa", "debit": "simplii_debit"}

logger = logging.getLogger("download_simplii_transactions")


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Download Simplii account activity without loading it"
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
    # Discovery proved the export form applies a From and a To date, so the end
    # of the range is settable; today keeps the default identical to before.
    parser.add_argument(
        "--until",
        type=date.fromisoformat,
        default=None,
        metavar="YYYY-MM-DD",
        help="Newest transaction date to keep (default: today)",
    )
    return parser.parse_args(argv)


def credentials() -> tuple[str, str]:
    values = tuple(
        os.environ.get(name, "") for name in ("SIMPLII_USER", "SIMPLII_PASSWORD")
    )
    if not all(values):
        raise RuntimeError(
            "SIMPLII_USER and SIMPLII_PASSWORD must be configured locally"
        )
    return values


def last_four_override() -> str | None:
    return os.environ.get("SIMPLII_ACCOUNT") or None


def destination(output_dir: Path, account: str, since: date) -> Path:
    name = f"simplii-{account}-{since.isoformat()}-{date.today().isoformat()}.csv"
    return output_dir.expanduser().resolve() / name


async def run_scrape(
    account: str,
    since: date,
    until: date,
    user: str,
    password: str,
    last_four: str | None,
) -> Path:
    """Seam over the Simplii BrowserOS bridge, which owns the browser session."""
    from scripts.simplii_browseros_bridge import run_download

    return Path(await run_download(account, since, until, user, password, last_four))


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
    last_four = last_four_override()
    target = destination(output_dir, account, since)
    if target.exists():
        raise FileExistsError(f"Refusing to overwrite existing file: {target}")
    logger.info("Scraping Simplii %s activity since %s...", account, since)
    downloaded = asyncio.run(
        run_scrape(account, since, until, user, password, last_four)
    )
    saved = move_download(downloaded, target.parent, target.name)
    try:
        validate_download(saved, ACCOUNTS[account])
    except Exception:
        saved.unlink(missing_ok=True)
        raise
    logger.info("Downloaded Simplii %s activity: %s", account, saved)
    logger.info("Load manually: %s", loader_command(saved, database, ACCOUNTS[account]))
    return saved


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

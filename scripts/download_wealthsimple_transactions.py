#!/usr/bin/env python3
"""Download Wealthsimple chequing or credit card activity through BrowserOS neo."""

import argparse
import asyncio
import logging
import os
import shlex
from datetime import date
from pathlib import Path

from config import Config
from scripts.wealthsimple_browseros_bridge import run_scrape, write_csv

ACCOUNTS = {"debit": "ws_debit", "credit": "ws_credit"}

logger = logging.getLogger("download_wealthsimple_transactions")


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Download Wealthsimple activity without loading it"
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
    return parser.parse_args(argv)


def credentials() -> tuple[str, str, str]:
    values = tuple(
        os.environ.get(name, "")
        for name in ("WS_EMAIL", "WS_PASSWORD", "WS_TOTP_SECRET")
    )
    if not all(values):
        raise RuntimeError(
            "WS_EMAIL, WS_PASSWORD and WS_TOTP_SECRET must be configured locally"
        )
    return values


def account_link(config: Config, account: str) -> str:
    link = config.ws_debt_link if account == "debit" else config.ws_credit_link
    if not link:
        name = "WS_DEBIT_LINK" if account == "debit" else "WS_CREDIT_LINK"
        raise RuntimeError(f"{name} must be configured locally")
    return link


def destination(output_dir: Path, account: str, since: date) -> Path:
    name = f"ws-{account}-{since.isoformat()}-{date.today().isoformat()}.csv"
    return output_dir.expanduser().resolve() / name


def validate_download(path: Path, account: str) -> None:
    from sources.registry import get_card_class

    columns = get_card_class(ACCOUNTS[account])(str(path)).get_df().columns
    expected = ["date", "merchant", "cost", "cc_category"]
    if columns != expected:
        raise RuntimeError(
            f"Downloaded Wealthsimple file has unsupported standardized columns: {columns}"
        )


def loader_command(path: Path, account: str, database: str) -> str:
    return shlex.join(
        [
            "uv",
            "run",
            "--frozen",
            "python",
            "load-transactions.py",
            "--type",
            ACCOUNTS[account],
            "--filepath",
            str(path),
            "--database",
            database,
        ]
    )


def run(output_dir: Path, database: str, account: str, since: date) -> Path:
    config = Config()
    link = account_link(config, account)
    email, password, totp_secret = credentials()
    target = destination(output_dir, account, since)
    if target.exists():
        raise FileExistsError(f"Refusing to overwrite existing file: {target}")
    logger.info("Scraping Wealthsimple %s activity since %s...", account, since)
    rows = asyncio.run(run_scrape(link, since, email, password, totp_secret))
    saved = write_csv(rows, target)
    try:
        validate_download(saved, account)
    except Exception:
        saved.unlink(missing_ok=True)
        raise
    logger.info("Downloaded %d Wealthsimple rows: %s", len(rows), saved)
    logger.info("Load manually: %s", loader_command(saved, account, database))
    return saved


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s-%(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
    args = parse_args(argv)
    try:
        run(args.output_dir, args.database, args.account, args.since)
    except KeyboardInterrupt:
        logger.warning("Interrupted.")
        return 130
    except Exception as exc:
        logger.error("ERROR: %s", exc)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

#!/usr/bin/env python3
"""Download CIBC Mastercard activity without loading transactions."""

import argparse
import asyncio
import csv
import logging
import os
import re
from datetime import date, datetime
from pathlib import Path

from dotenv import load_dotenv

from sources.csv.cibc_mc import CibcMcStatement

from scripts.browser_common import loader_command, validate_download
from scripts.download_rogers_transactions import discard

load_dotenv()
logger = logging.getLogger("download_cibc_transactions")
HISTORY_START = date(2011, 12, 11)
FRESHNESS_WARNING = "CIBC excludes the current day and 2 previous business days from CSV downloads; this export does not establish complete coverage through --until."


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument(
        "--database", choices=("finance", "parents_finance"), required=True
    )
    parser.add_argument(
        "--since", type=date.fromisoformat, required=True, metavar="YYYY-MM-DD"
    )
    parser.add_argument("--until", type=date.fromisoformat, metavar="YYYY-MM-DD")
    return parser.parse_args(argv)


def credentials() -> tuple[str, str]:
    values = tuple(os.environ.get(name, "") for name in ("CIBC_USER", "CIBC_PASSWORD"))
    if not all(values):
        raise RuntimeError("CIBC_USER and CIBC_PASSWORD must be configured locally")
    return values


def combine_exports(exports: list[Path], since: date, until: date, target: Path) -> int:
    kept = []
    for export in exports:
        with export.open(newline="", encoding="utf-8-sig") as handle:
            for row in csv.reader(handle):
                try:
                    if len(row) != 5:
                        raise ValueError
                    row_date = datetime.strptime(row[0], "%Y-%m-%d").date()
                    if not any(row[2:4]):
                        raise ValueError
                    for amount in row[2:4]:
                        if amount and not re.fullmatch(
                            r"-?[0-9]+(?:\.[0-9]+)?", amount
                        ):
                            raise ValueError
                except ValueError:
                    raise RuntimeError(
                        "CIBC export has an invalid five-column row, date, or amount"
                    ) from None
                if since <= row_date <= until:
                    kept.append(row)
    if not kept:
        raise RuntimeError("CIBC export has no transactions in the requested range")
    target.parent.mkdir(parents=True, exist_ok=True)
    created = False
    try:
        with target.open("x", newline="", encoding="utf-8") as handle:
            created = True
            os.chmod(target, 0o600)
            csv.writer(handle, lineterminator="\r\n").writerows(kept)
    except BaseException:
        if created:
            target.unlink(missing_ok=True)
        raise
    return len(kept)


async def run_scrape(since: date, until: date, user: str, password: str) -> list[Path]:
    from scripts.cibc_browseros_bridge import run_download

    return await run_download(since, until, user, password)


def run(
    output_dir: Path, database: str, since: date, until: date | None = None
) -> Path:
    until = until or date.today()
    if until < since:
        raise ValueError("--until must not be earlier than --since")
    if since < HISTORY_START:
        raise ValueError(
            "CIBC history starts December 11, 2011; choose a later --since"
        )
    if database not in ("finance", "parents_finance"):
        raise ValueError("Unsupported database")
    output_dir = output_dir.expanduser().resolve()
    if output_dir.is_relative_to(Path(__file__).resolve().parents[1]):
        raise ValueError("Keep --output-dir outside the repository")
    target = output_dir / f"cibc-mc-{since.isoformat()}-{until.isoformat()}.csv"
    if target.exists():
        raise FileExistsError(f"Refusing to overwrite existing file: {target}")
    user, password = credentials()
    logger.warning(FRESHNESS_WARNING)
    try:
        exports = asyncio.run(run_scrape(since, until, user, password))
    except KeyboardInterrupt:
        raise
    except Exception as exc:
        from scripts.cibc_browseros_bridge import CibcError

        if isinstance(exc, CibcError):
            raise
        raise RuntimeError(
            "CIBC browser download failed; check CIBC security/export page and rerun"
        ) from None
    try:
        rows = combine_exports(exports, since, until, target)
    finally:
        discard(exports)
    try:
        validate_download(target, "cibc_mc")
        cost = CibcMcStatement(str(target)).get_df()["cost"]
        if not cost.dtype.is_numeric() or not cost.is_finite().all():
            raise ValueError("CIBC parsed cost must be numeric and finite")
    except BaseException:
        target.unlink(missing_ok=True)
        raise RuntimeError("Downloaded CIBC CSV failed parser validation") from None
    logger.info("Downloaded CIBC activity: %s (%s rows)", target, rows)
    logger.info("Load manually: %s", loader_command(target, database, "cibc_mc"))
    return target


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(name)s: %(message)s")
    args = parse_args(argv)
    try:
        run(args.output_dir, args.database, args.since, args.until)
    except KeyboardInterrupt:
        logger.warning("Interrupted.")
        return 130
    except Exception as exc:
        logger.error("ERROR: %s", exc)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

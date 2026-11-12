#!/usr/bin/env python3
"""Download Rogers Bank credit card activity through BrowserOS neo."""

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

CARD_TYPE = "rogers"
DATE_FORMAT = "%Y-%m-%d"

logger = logging.getLogger("download_rogers_transactions")


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Download Rogers Bank account activity without loading it"
    )
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument(
        "--database", choices=("finance", "parents_finance"), required=True
    )
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
    values = tuple(
        os.environ.get(name, "") for name in ("ROGERS_USER", "ROGERS_PASSWORD")
    )
    if not all(values):
        raise RuntimeError("ROGERS_USER and ROGERS_PASSWORD must be configured locally")
    return values


def destination(output_dir: Path, since: date) -> Path:
    name = f"rogers-{since.isoformat()}-{date.today().isoformat()}.csv"
    return output_dir.expanduser().resolve() / name


def combine_exports(exports: list[Path], since: date, until: date, target: Path) -> int:
    """Write the exports' rows dated since..until into one new file.

    The Rogers export is one statement month per file, and a statement month
    can start before `since`, so rows are filtered here. All files share the
    header row; it is written once. Rows are kept exactly as Rogers wrote
    them, in file order, and an existing target is refused.
    """
    header: list[str] | None = None
    kept: list[list[str]] = []
    for export in exports:
        with export.open(newline="", encoding="utf-8-sig") as handle:
            reader = csv.reader(handle)
            file_header = next(reader, None)
            if file_header is None:
                continue
            if header is None:
                header = file_header
            elif file_header != header:
                raise RuntimeError("Rogers exports do not share one header")
            for row in reader:
                if not row:
                    continue
                try:
                    row_date = datetime.strptime(row[0], DATE_FORMAT).date()
                except ValueError as exc:
                    raise RuntimeError(
                        "Rogers export has a row with an unexpected date format"
                    ) from exc
                if since <= row_date <= until:
                    kept.append(row)
    if header is None or not kept:
        raise RuntimeError(f"Rogers export has no transactions from {since} to {until}")
    target.parent.mkdir(parents=True, exist_ok=True)
    try:
        with target.open("x", newline="", encoding="utf-8") as handle:
            writer = csv.writer(handle, lineterminator="\r\n")
            writer.writerow(header)
            writer.writerows(kept)
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


async def run_scrape(since: date, until: date, user: str, password: str) -> list[Path]:
    """Seam over the Rogers BrowserOS bridge, which owns the browser session.

    Returns one raw CSV per statement month. They sit in a private temporary
    folder.
    """
    from scripts.rogers_browseros_bridge import run_download

    return [Path(path) for path in await run_download(since, until, user, password)]


def run(
    output_dir: Path,
    database: str,
    since: date,
    until: date | None = None,
) -> Path:
    until = until or date.today()
    if until < since:
        raise ValueError("--until must not be earlier than --since")
    user, password = credentials()
    target = destination(output_dir, since)
    if target.exists():
        raise FileExistsError(f"Refusing to overwrite existing file: {target}")
    logger.info("Downloading Rogers Bank activity since %s...", since)
    exports = asyncio.run(run_scrape(since, until, user, password))
    try:
        rows = combine_exports(exports, since, until, target)
    finally:
        discard(exports)
    try:
        validate_download(target, CARD_TYPE)
    except Exception:
        target.unlink(missing_ok=True)
        raise
    logger.info("Downloaded Rogers Bank activity: %s (%s rows)", target, rows)
    logger.info("Load manually: %s", loader_command(target, database, CARD_TYPE))
    return target


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s-%(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
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

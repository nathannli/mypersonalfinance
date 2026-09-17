"""Research unresolved merchants with TinyFish and freeze evidence packets.

Phase one of the two-phase workflow. This is the only entry point that may call
TinyFish (V3): it discovers `finance` transactions that deterministic matching
cannot resolve, researches each distinct merchant once, and freezes a packet for
human review. Transaction loading only ever reads frozen packets and never
calls the web.

This CLI writes no expense, category, subcategory, deletion, or auto-match
rows and makes no OpenCodex request (V4).

Usage:
    python research-transaction-merchants.py --type amex \
        --filepath ~/Downloads/amex/amex-latest.csv --database finance

    python research-transaction-merchants.py --type amex \
        --folder ~/Downloads/amex/ --database finance --refresh
"""

from __future__ import annotations

import argparse
import os
from collections.abc import Mapping, Sequence

import polars as pl
from dotenv import load_dotenv

from cli.transaction_loader_cli import TransactionLoaderCLI
from db.my_finance import MyFinanceDB
from services.research_packets import ResearchRunStatus, review_record_for
from services.research_runner import (
    ResearchRunSummary,
    discover_targets,
    run_targets,
    summarize,
)
from services.tinyfish_research import (
    DEFAULT_RESEARCH_LANGUAGE,
    DEFAULT_RESEARCH_LOCATION,
    TINYFISH_API_KEY_ENV,
    TinyFishClient,
    TinyFishResearchConfig,
)
from services.transaction_loader import TransactionLoader
from utils.repo_paths import repo_root

SUPPORTED_DATABASE = "finance"
RECORD_SEPARATOR = "-" * 72


def build_parser() -> argparse.ArgumentParser:
    parser = TransactionLoaderCLI().parser
    parser.description = (
        "Research unresolved finance merchants with TinyFish and freeze "
        "evidence packets for review"
    )
    parser.epilog = f"""
Two-phase workflow: this command calls TinyFish and freezes evidence;
load-transactions.py only reads frozen packets.

Requires {TINYFISH_API_KEY_ENV} in the repository .env. Only --database
{SUPPORTED_DATABASE} is supported.

Usage Examples:

  Research every unresolved merchant in a folder:
    python research-transaction-merchants.py --type amex \\
        --folder ~/Downloads/amex/ --database finance

  Re-research and replace existing packets:
    python research-transaction-merchants.py --type amex \\
        --folder ~/Downloads/amex/ --database finance --refresh
"""
    parser.add_argument(
        "--refresh",
        action="store_true",
        help=(
            "Re-research merchants that already have a packet. A successful "
            "refresh replaces the packet and returns it to pending review; a "
            "failed refresh keeps the existing packet and its approval."
        ),
    )
    return parser


def resolve_api_key() -> str:
    """Read the TinyFish key from the repository .env, lazily (V39, V48).

    Only this entry point reads or validates this key. It is deliberately never
    resolved during Config construction, so the load, cron, and gold-validation
    paths keep working with no TinyFish credentials at all.
    """

    root = repo_root()
    load_dotenv(root / ".env")
    key = os.environ.get(TINYFISH_API_KEY_ENV, "")
    if not key.strip():
        raise ValueError(
            f"{TINYFISH_API_KEY_ENV} is required to research merchants. "
            f"Add it to {root / '.env'}."
        )
    return key.strip()


def load_rows(card_type: str, files: Sequence[str | None]) -> list[Mapping]:
    """Read every requested input through the same path the loader uses."""

    loader = TransactionLoader()
    rows: list[Mapping] = []
    for file_path in files:
        frame: pl.DataFrame = loader.load(card_type, file_path)
        rows.extend(frame.iter_rows(named=True))
    return rows


def coverage_line(packet_ids: Sequence[str]) -> str:
    approved = rejected = pending = 0
    for packet_id in packet_ids:
        record = review_record_for(packet_id)
        if record is None:
            pending += 1
        elif record.status.value == "approved":
            approved += 1
        else:
            rejected += 1
    return (
        f"packets this run: {len(packet_ids)} "
        f"(pending review {pending}, approved {approved}, rejected {rejected})"
    )


def print_summary(summary: ResearchRunSummary) -> None:
    """Report the run without leaking credentials or raw page content (V34)."""

    print(f"\n{RECORD_SEPARATOR}")
    print(f"Research run: {summary.status.value.upper()}")
    print(RECORD_SEPARATOR)
    print(f"  targets       : {summary.total}")
    print(f"  reused        : {summary.reused} (valid packet already present)")
    print(f"  researched    : {summary.researched}")
    print(f"  refreshed     : {summary.refreshed}")
    print(f"  failed        : {summary.failed}")

    if summary.failures:
        print("\nFailures:")
        for result in summary.failures:
            reason = result.failure_reason.value if result.failure_reason else "unknown"
            print(
                f"  {result.normalized_merchant}: "
                f"{result.operation.value} failed ({reason})"
            )

    if summary.refresh_failures:
        print("\nRefresh failures (existing evidence left intact, V60):")
        for result in summary.refresh_failures:
            reason = result.failure_reason.value if result.failure_reason else "unknown"
            print(
                f"  {result.normalized_merchant}: {reason}"
                + (
                    " (previous packet preserved)"
                    if result.preserved_previous_packet
                    else ""
                )
            )


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    if args.database != SUPPORTED_DATABASE:
        print(
            f"ERROR: research supports only --database {SUPPORTED_DATABASE}; "
            f"got {args.database}. Enrichment applies to finance only (V2)."
        )
        return 1

    # Reuse the loader's own acceptance rules so the two commands cannot drift.
    loader_cli = TransactionLoaderCLI()
    try:
        loader_cli._validate_arguments(args.type, args.filepath, args.folder)
        files = loader_cli._build_file_list(args.type, args.filepath, args.folder)
    except ValueError as error:
        print(f"ERROR: {error}")
        return 1

    try:
        api_key = resolve_api_key()
    except ValueError as error:
        print(f"ERROR: {error}")
        return 1

    print(f"Reading {args.type} input...")
    try:
        rows = load_rows(args.type, files)
    except Exception as error:  # noqa: BLE001 - an input failure fails the run
        print(f"ERROR: could not read input: {error}")
        return 1
    print(f"Read {len(rows)} rows from {len(files)} input(s)")

    print("Loading finance taxonomy...")
    database = MyFinanceDB(debug=False)
    choices = database.get_categorization_choices()

    # Read-only discovery: rows the deterministic resolver cannot place (V4).
    targets = discover_targets(
        rows,
        card_type=args.type,
        choices=choices,
        auto_match=database.get_auto_match_category,
    )
    print(f"Discovered {len(targets)} distinct unresolved merchant(s)")

    if not targets:
        summary = summarize(())
        print_summary(summary)
        print("\nNothing to research.")
        return summary.exit_code

    config = TinyFishResearchConfig(
        api_key=api_key,
        location=DEFAULT_RESEARCH_LOCATION,
        language=DEFAULT_RESEARCH_LANGUAGE,
    )
    client = TinyFishClient(config)

    print(f"Researching {len(targets)} merchant(s)...\n")
    results = run_targets(
        targets,
        client=client,
        refresh=bool(args.refresh),
    )
    summary = summarize(results)
    print_summary(summary)

    print(f"\n{coverage_line([result.packet_id for result in results])}")
    if summary.status is not ResearchRunStatus.FAILED:
        print(
            "\nReview with: python review-transaction-research.py "
            "(approve or reject each exact packet hash before it can be used)"
        )
    print(f"{RECORD_SEPARATOR}")

    if client.circuit_open:
        reason = client.circuit_reason
        print(
            f"\nTinyFish circuit opened"
            f"{f' ({reason.value})' if reason else ''}; "
            "remaining merchants were not requested."
        )

    return summary.exit_code


if __name__ == "__main__":
    raise SystemExit(main())

"""Offline review of frozen TinyFish research packets.

Phase one-and-a-half: a human reads the frozen evidence for a merchant and
approves or rejects the exact packet hash. Nothing here is automatic, and this
command makes no TinyFish request, no OpenCodex request, and no database write:
it reads packets and writes only the ignored review artifact.

Only an approval of an exact `packet_sha256` makes a packet eligible to support
`select` or `suggest_new` (V45). Pending, rejected, stale, and failure packets
never do.

With no flags on a terminal it walks the pending packets one at a time and
records a decision for each. On a non-interactive stdin it prints them all
instead, so a script or a log gets the listing. ``--interactive`` and ``--list``
force either behaviour.

``--type`` with ``--filepath`` or ``--folder`` reads the same statement rows
the research pass read and shows the transaction dates behind each merchant, so
the evidence is judged against the purchase that prompted it. Those flags are
optional and change no decision: they only add context to the printed packet.

Usage:
    python review-transaction-research.py
    python review-transaction-research.py --interactive
    python review-transaction-research.py --type amex --folder ~/Downloads/amex/
    python review-transaction-research.py --list
    python review-transaction-research.py --approve <packet_id>
    python review-transaction-research.py --reject <packet_id> --reason "wrong company"
    python review-transaction-research.py --suggestions
"""

from __future__ import annotations

import argparse
import os
import sys
from collections.abc import Mapping, Sequence
from datetime import date

from services.research_packets import (
    PacketReviewRecord,
    PacketReviewStatus,
    PacketStatus,
    ResearchPacket,
    ResearchPacketError,
    list_packet_ids,
    load_packet,
    load_suggestions,
    review_record_for,
    record_review,
    utc_now,
)
from services.transaction_categorization import normalize_context_text
from services.transaction_loader import TransactionLoader
from sources.registry import get_card_type_names, requires_file

EXCERPT_CHARS = 400
MAX_SHOWN_DATES = 10
RECORD_SEPARATOR = "-" * 72


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Review frozen TinyFish merchant research packets and approve or "
            "reject exact packet hashes"
        ),
        epilog="""
This command is offline: it reads packets and writes only the review artifact.
A packet must be approved here before transaction loading may use it.

Usage Examples:

  List packets awaiting review:
    python review-transaction-research.py

  Walk pending packets one at a time and record each decision:
    python review-transaction-research.py --interactive

  Show the transaction dates behind each merchant while reviewing:
    python review-transaction-research.py --interactive \\
        --type amex --folder ~/Downloads/amex/

  List recorded category suggestions:
    python review-transaction-research.py --suggestions

  Approve one exact packet:
    python review-transaction-research.py --approve <packet_id>

  Reject one exact packet:
    python review-transaction-research.py --reject <packet_id> \\
        --reason "evidence is about a different company"
""",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--approve", metavar="PACKET_ID", help="Approve this packet")
    parser.add_argument("--reject", metavar="PACKET_ID", help="Reject this packet")
    parser.add_argument(
        "--reason",
        help="Why this packet is rejected (required with --reject)",
    )
    parser.add_argument(
        "--suggestions",
        action="store_true",
        help="List the recorded review-only category suggestions",
    )
    parser.add_argument(
        "--interactive",
        action="store_true",
        help="Walk pending packets one at a time and record each decision",
    )
    parser.add_argument(
        "--list",
        dest="list_only",
        action="store_true",
        help="Print every pending packet at once instead of walking them",
    )
    parser.add_argument(
        "--type",
        choices=get_card_type_names(),
        help=(
            "Card type whose statement rows hold the transaction dates to show. "
            "Requires --filepath or --folder for a file-based card."
        ),
    )
    parser.add_argument(
        "--filepath", help="Statement file to read transaction dates from"
    )
    parser.add_argument(
        "--folder", help="Folder of statement files to read transaction dates from"
    )
    return parser


def load_merchant_dates(args: argparse.Namespace) -> dict[str, tuple[date, ...]]:
    """Group statement dates by normalized merchant, read offline.

    Returns an empty mapping when no card type was named, so the review output
    is unchanged for callers that pass no statement arguments.
    """

    if not args.type:
        return {}
    if args.filepath and args.folder:
        raise ValueError("Cannot provide both --filepath and --folder.")
    if requires_file(args.type):
        if not args.filepath and not args.folder:
            raise ValueError(
                f"Please provide either --filepath or --folder for {args.type} "
                "transactions"
            )
    elif args.filepath or args.folder:
        raise ValueError(
            f"{args.type} doesn't use csv files, no need to provide --filepath "
            "or --folder"
        )

    if args.folder:
        if not os.path.isdir(args.folder):
            raise ValueError(f"Folder does not exist: {args.folder}")
        files: Sequence[str | None] = [
            os.path.join(args.folder, name)
            for name in sorted(os.listdir(args.folder))
            if os.path.isfile(os.path.join(args.folder, name))
        ]
        if not files:
            raise ValueError(f"Folder is empty: {args.folder}")
        print(f"Found {len(files)} files in folder: {args.folder}")
    elif args.filepath:
        files = [args.filepath]
    else:
        files = [None]

    loader = TransactionLoader()
    grouped: dict[str, set[date]] = {}
    for file_path in files:
        for row in loader.load(args.type, file_path).iter_rows(named=True):
            merchant = row.get("merchant")
            if not isinstance(merchant, str) or not merchant.strip():
                continue
            grouped.setdefault(normalize_context_text(merchant), set()).add(row["date"])
    return {merchant: tuple(sorted(dates)) for merchant, dates in grouped.items()}


def resolve_packet_id(value: str) -> str:
    """Accept either a full packet id or an unambiguous prefix."""

    candidate = value.strip()
    if candidate in list_packet_ids():
        return candidate
    matches = [
        packet_id for packet_id in list_packet_ids() if packet_id.startswith(candidate)
    ]
    if len(matches) == 1:
        return matches[0]
    if not matches:
        raise ValueError(f"no stored packet matches {value!r}")
    raise ValueError(f"{value!r} is ambiguous: {len(matches)} packets match")


def excerpt(text: str) -> str:
    collapsed = " ".join(text.split())
    if len(collapsed) <= EXCERPT_CHARS:
        return collapsed
    return f"{collapsed[:EXCERPT_CHARS]}..."


def format_dates(dates: Sequence[date]) -> str:
    """Render a merchant's transaction dates, bounded."""

    shown = ", ".join(value.isoformat() for value in dates[:MAX_SHOWN_DATES])
    remaining = len(dates) - MAX_SHOWN_DATES
    if remaining > 0:
        return f"{shown} (+{remaining} more)"
    return shown


def render_packet(
    packet: ResearchPacket,
    *,
    index: int | None = None,
    merchant_dates: Mapping[str, tuple[date, ...]] | None = None,
) -> None:
    """Render one packet's evidence, bounded and without any page dump."""

    record = review_record_for(packet.packet_id)
    header = f"merchant: {packet.normalized_merchant}"
    if index is not None:
        header = f"[{index}] {header}"
    print(header)
    print(f"    packet_id    : {packet.packet_id}")
    print(f"    packet_sha256: {packet.packet_sha256}")
    print(f"    derived query: {packet.derived_query}")
    print(f"    searched at  : {packet.searched_at}")
    print(
        f"    versions     : schema {packet.schema_version}, "
        f"query {packet.query_version}"
    )
    if record is not None:
        print(
            f"    review       : {record.status.value} at {record.reviewed_at}"
            + (f" ({record.reason})" if record.reason else "")
        )
    if merchant_dates is not None:
        dates = merchant_dates.get(packet.normalized_merchant, ())
        label = format_dates(dates) if dates else "none in the given statements"
        print(f"    transactions : {label}")

    print(f"    search results ({len(packet.search_results)}):")
    for result in packet.search_results:
        print(f"      {result.position}. {result.title}")
        print(f"         {result.url}")
        if result.snippet:
            print(f"         {excerpt(result.snippet)}")

    print(f"    fetched pages ({len(packet.fetched_pages)}):")
    for page in packet.fetched_pages:
        print(f"      {page.url} -> {page.final_url}")
        if page.title:
            print(f"         title: {page.title}")
        if page.description:
            print(f"         desc : {excerpt(page.description)}")
        print(f"         text : {excerpt(page.text)}")
        tokens = ", ".join(page.relevance_matched_tokens) or "none"
        print(f"         relevance tokens: {tokens}")
    print()


def is_pending(packet: ResearchPacket) -> bool:
    """Pending until a record exists that still binds this exact hash (V52)."""

    record = review_record_for(packet.packet_id)
    if record is None:
        return True
    return record.packet_sha256 != packet.packet_sha256


def collect_packets() -> tuple[list[ResearchPacket], dict[str, int], list[str]]:
    """Split every stored packet into pending, decided and unreviewable.

    Returns the pending packets, the counts of every other state, and any
    warnings for packets that could not be read, so a caller can present them
    without a second pass over the store.
    """

    packet_ids = list_packet_ids()
    pending: list[ResearchPacket] = []
    counts = {"stored": len(packet_ids), "approved": 0, "rejected": 0, "failed": 0}
    warnings: list[str] = []

    for packet_id in packet_ids:
        try:
            packet = load_packet(packet_id)
        except ResearchPacketError as error:
            # Surface a damaged packet instead of hiding it behind the listing.
            warnings.append(f"WARNING: packet {packet_id} could not be read: {error}")
            continue
        if packet.status is PacketStatus.FAILED:
            # V55: failure packets are never reviewable.
            counts["failed"] += 1
            continue
        if is_pending(packet):
            # A record that no longer binds this exact hash is pending too, so
            # a packet whose approval was superseded is never reported approved.
            pending.append(packet)
            continue
        record = review_record_for(packet_id)
        if record is not None and record.status is PacketReviewStatus.APPROVED:
            counts["approved"] += 1
        else:
            counts["rejected"] += 1

    return pending, counts, warnings


def print_summary(counts: dict[str, int], broken: int) -> None:
    print(f"{RECORD_SEPARATOR}")
    print(
        f"Packets: {counts['stored']} stored, {counts['pending']} pending review, "
        f"{counts['approved']} approved, {counts['rejected']} rejected, "
        f"{counts['failed']} failed" + (f", {broken} unreadable" if broken else "")
    )
    print(RECORD_SEPARATOR)


def list_packets(merchant_dates: Mapping[str, tuple[date, ...]] | None = None) -> int:
    packet_ids = list_packet_ids()
    if not packet_ids:
        print("No research packets found. Run research-transaction-merchants.py first.")
        return 0

    pending, counts, warnings = collect_packets()
    for warning in warnings:
        print(warning)
    counts["pending"] = len(pending)
    print_summary(counts, broken=len(warnings))

    if not pending:
        print("\nNothing awaiting review.")
        return 0

    print(f"\n{len(pending)} packet(s) awaiting review:\n")
    for index, packet in enumerate(pending, start=1):
        render_packet(packet, index=index, merchant_dates=merchant_dates)
    print(
        "Walk them one at a time with: "
        "python review-transaction-research.py --interactive\n"
        "Approve with: python review-transaction-research.py --approve <packet_id>\n"
        "Reject with:  python review-transaction-research.py --reject <packet_id> "
        '--reason "..."'
    )
    return 0


def prompt(message: str) -> str:
    """Read one line, tolerating a closed or non-interactive stdin."""

    try:
        return input(message).strip()
    except EOFError:
        return ""


def review_interactively(
    merchant_dates: Mapping[str, tuple[date, ...]] | None = None,
) -> int:
    """Walk the pending packets one at a time, recording a decision per packet.

    Each packet is shown on its own so the evidence for one merchant is read
    before the next is opened. The decision path is the same `decide` the
    one-shot flags use, so an interactive run and a scripted one write the
    same records.
    """

    packet_ids = list_packet_ids()
    if not packet_ids:
        print("No research packets found. Run research-transaction-merchants.py first.")
        return 0

    pending, counts, warnings = collect_packets()
    for warning in warnings:
        print(warning)
    counts["pending"] = len(pending)
    print_summary(counts, broken=len(warnings))

    if not pending:
        print("\nNothing awaiting review.")
        return 0

    print(
        f"\nWalking {len(pending)} packet(s). One packet at a time; the next is\n"
        "shown after you decide. Type the highlighted letter or the whole word.\n"
    )

    decided = 0
    for index, packet in enumerate(pending, start=1):
        print(RECORD_SEPARATOR)
        render_packet(packet, index=index, merchant_dates=merchant_dates)

        while True:
            answer = prompt(
                f"[{index}/{len(pending)}] (a)pprove / (r)eject / (s)kip / (q)uit? "
            ).lower()
            if answer in {"a", "approve"}:
                code = decide(packet.packet_id, PacketReviewStatus.APPROVED, None)
                decided += 1 if code == 0 else 0
                break
            if answer in {"r", "reject"}:
                reason = prompt("reason (required): ")
                if not reason:
                    # An empty reason would write a rejection nobody can act on.
                    print("A rejection needs a reason. Try again, or s to skip.")
                    continue
                code = decide(packet.packet_id, PacketReviewStatus.REJECTED, reason)
                decided += 1 if code == 0 else 0
                break
            if answer in {"s", "skip"}:
                print("Skipped; it stays pending.\n")
                break
            if answer in {"q", "quit"}:
                remaining = len(pending) - index + 1
                print(
                    f"\nStopped. {decided} decision(s) recorded, "
                    f"{remaining} packet(s) left pending."
                )
                return 0
            print("Unrecognized. Use (a)pprove, (r)eject, (s)kip or (q)uit.\n")

    print(f"\nDone. {decided} decision(s) recorded.")
    return 0


def list_suggestions() -> int:
    """Render the grouped suggestion artifact, the review half of `suggest_new`.

    `suggest_new` is the one action that persists something, so the artifact it
    writes is the only place a proposal can be read back. This is local
    interactive output, so the full proposal and its citations are shown; a
    cron or persistent log reports identifiers only (V35).
    """

    try:
        grouped = load_suggestions()
    except ResearchPacketError as error:
        print(f"ERROR: {error}")
        return 1

    total = sum(len(items) for items in grouped.values())
    print(f"{RECORD_SEPARATOR}")
    print(f"Category suggestions: {total} across {len(grouped)} merchant(s)")
    print(RECORD_SEPARATOR)
    if not grouped:
        print("\nNo category suggestions recorded.")
        return 0

    for merchant, suggestions in sorted(grouped.items()):
        print(f"\n{merchant} ({len(suggestions)}):")
        for suggestion in suggestions:
            print(f"  - {suggestion.suggestion_id}")
            print(f"      category   : {suggestion.category_name}")
            print(f"      subcategory: {suggestion.subcategory_name}")
            if suggestion.parent_category_id is not None:
                print(f"      parent id  : {suggestion.parent_category_id}")
            if suggestion.rationale:
                print(f"      rationale  : {excerpt(suggestion.rationale)}")
            for url in suggestion.evidence_urls:
                print(f"      evidence   : {url}")
            print(f"      packet     : {suggestion.research_packet_sha256}")
            print(f"      contexts   : {len(suggestion.context_fingerprints)}")
    print(
        "\nSuggestions are review-only. Nothing here created or changed a "
        "category, subcategory, expense, or auto-match row."
    )
    return 0


def load_for_review(packet_id: str) -> ResearchPacket:
    """Load a packet that is actually eligible for a decision."""

    packet = load_packet(packet_id)
    if packet.status is not PacketStatus.COMPLETE:
        raise ValueError(
            "this is a failure packet, and failure packets are never reviewable (V55)"
        )
    if packet.is_stale():
        raise ValueError(
            "this packet was written under different schema/query versions; "
            "refresh it with research-transaction-merchants.py --refresh and "
            "review the new hash (V57)"
        )
    return packet


def decide(packet_id: str, status: PacketReviewStatus, reason: str | None) -> int:
    try:
        resolved = resolve_packet_id(packet_id)
        packet = load_for_review(resolved)
        record_review(
            PacketReviewRecord(
                packet_id=packet.packet_id,
                packet_sha256=packet.packet_sha256,
                status=status,
                reviewed_at=utc_now(),
                reason=reason,
            )
        )
    except (ResearchPacketError, ValueError) as error:
        print(f"ERROR: {error}")
        return 1

    print(f"{status.value.upper()}: {packet.normalized_merchant}")
    print(f"  packet_id    : {packet.packet_id}")
    print(f"  packet_sha256: {packet.packet_sha256}")
    if reason:
        print(f"  reason       : {reason}")
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    if args.approve and args.reject:
        print("ERROR: pass either --approve or --reject, not both")
        return 1
    if args.reason and not args.reject:
        print("ERROR: --reason is only valid with --reject")
        return 1
    if args.suggestions and (args.approve or args.reject or args.reason):
        print("ERROR: --suggestions cannot be combined with a decision")
        return 1
    if args.interactive and args.list_only:
        print("ERROR: pass either --interactive or --list, not both")
        return 1
    if args.interactive and (args.approve or args.reject or args.reason):
        print("ERROR: --interactive cannot be combined with a single-packet decision")
        return 1
    if args.suggestions:
        return list_suggestions()

    try:
        merchant_dates = load_merchant_dates(args)
    except (ValueError, OSError) as error:
        print(f"ERROR: {error}")
        return 1

    if args.interactive:
        return review_interactively(merchant_dates)
    if args.approve:
        return decide(args.approve, PacketReviewStatus.APPROVED, None)
    if args.reject:
        if not args.reason or not args.reason.strip():
            print("ERROR: --reject requires a --reason")
            return 1
        return decide(args.reject, PacketReviewStatus.REJECTED, args.reason.strip())
    if args.list_only or not sys.stdin.isatty():
        # A piped or redirected stdin cannot answer, so it gets the listing.
        return list_packets(merchant_dates)
    return review_interactively(merchant_dates)


if __name__ == "__main__":
    raise SystemExit(main())

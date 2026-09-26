"""Offline review of frozen TinyFish research packets.

Phase one-and-a-half: a human reads the frozen evidence for a merchant and
approves or rejects the exact packet hash. Nothing here is automatic, and this
command makes no TinyFish request, no OpenCodex request, and no database write:
it reads packets and writes only the ignored review artifact.

Only an approval of an exact `packet_sha256` makes a packet eligible to support
`select` or `suggest_new` (V45). Pending, rejected, stale, and failure packets
never do.

Usage:
    python review-transaction-research.py
    python review-transaction-research.py --approve <packet_id>
    python review-transaction-research.py --reject <packet_id> --reason "wrong company"
    python review-transaction-research.py --suggestions
"""

from __future__ import annotations

import argparse
from collections.abc import Sequence

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

EXCERPT_CHARS = 400
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
    return parser


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


def render_packet(packet: ResearchPacket, *, index: int | None = None) -> None:
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


def list_packets() -> int:
    packet_ids = list_packet_ids()
    if not packet_ids:
        print("No research packets found. Run research-transaction-merchants.py first.")
        return 0

    pending: list[ResearchPacket] = []
    approved = rejected = failed = 0
    broken = 0

    for packet_id in packet_ids:
        try:
            packet = load_packet(packet_id)
        except ResearchPacketError as error:
            # Surface a damaged packet instead of hiding it behind the listing.
            print(f"WARNING: packet {packet_id} could not be read: {error}")
            broken += 1
            continue
        if packet.status is PacketStatus.FAILED:
            # V55: failure packets are never reviewable.
            failed += 1
            continue
        if is_pending(packet):
            # A record that no longer binds this exact hash is pending too, so
            # a packet whose approval was superseded is never reported approved.
            pending.append(packet)
            continue
        record = review_record_for(packet_id)
        if record is not None and record.status is PacketReviewStatus.APPROVED:
            approved += 1
        else:
            rejected += 1

    print(f"{RECORD_SEPARATOR}")
    print(
        f"Packets: {len(packet_ids)} stored, {len(pending)} pending review, "
        f"{approved} approved, {rejected} rejected, {failed} failed"
        + (f", {broken} unreadable" if broken else "")
    )
    print(RECORD_SEPARATOR)

    if not pending:
        print("\nNothing awaiting review.")
        return 0

    print(f"\n{len(pending)} packet(s) awaiting review:\n")
    for index, packet in enumerate(pending, start=1):
        render_packet(packet, index=index)
    print(
        "Approve with: python review-transaction-research.py --approve <packet_id>\n"
        "Reject with:  python review-transaction-research.py --reject <packet_id> "
        '--reason "..."'
    )
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
    if args.suggestions:
        return list_suggestions()

    if args.approve:
        return decide(args.approve, PacketReviewStatus.APPROVED, None)
    if args.reject:
        if not args.reason or not args.reason.strip():
            print("ERROR: --reject requires a --reason")
            return 1
        return decide(args.reject, PacketReviewStatus.REJECTED, args.reason.strip())
    return list_packets()


if __name__ == "__main__":
    raise SystemExit(main())

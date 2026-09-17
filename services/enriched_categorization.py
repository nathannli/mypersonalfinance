"""Resolve the frozen research packet that authorizes enriched categorization.

Read-only: this module writes nothing, opens no database, and makes no TinyFish
request. It turns the private packet store plus the review artifact into either
one approved complete packet or exactly one typed reason (V15, V41, V47, V55,
V57).
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from services.research_packets import (
    RESEARCH_EXECUTION_REASONS,
    PacketReviewStatus,
    PacketStatus,
    ResearchPacket,
    ResearchPacketError,
    ResearchPacketMalformedError,
    ResearchPacketTamperedError,
    load_packet_if_present,
    packet_id_for,
    review_record_for,
)
from services.transaction_categorization import UnresolvedReason


@dataclass(frozen=True)
class PacketResolution:
    """Exactly one of ``packet`` or ``reason`` is set (V25)."""

    packet: ResearchPacket | None = None
    reason: UnresolvedReason | None = None

    def __post_init__(self) -> None:
        if (self.packet is None) == (self.reason is None):
            raise ValueError(
                "PacketResolution requires exactly one of packet or reason"
            )

    @property
    def approved(self) -> bool:
        return self.packet is not None


def _reason(reason: UnresolvedReason) -> PacketResolution:
    return PacketResolution(reason=reason)


def resolve_approved_packet(
    normalized_merchant: str, *, root: Path | None = None
) -> PacketResolution:
    """Resolve one merchant's frozen packet, or the reason it cannot be used.

    Order is load-bearing: tampering is reported before staleness, and staleness
    before a failed packet's stored reason, so a version-drifted packet is
    always ``research_stale`` regardless of its status (V57). Only a genuinely
    absent file is ``research_missing`` (V55).
    """
    packet_id = packet_id_for(normalized_merchant)

    try:
        packet = load_packet_if_present(packet_id, root=root)
    except ResearchPacketTamperedError:
        # A recomputed hash that differs is tampering, never staleness.
        return _reason(UnresolvedReason.RESEARCH_TAMPERED)
    except ResearchPacketMalformedError:
        return _reason(UnresolvedReason.RESEARCH_MALFORMED)

    if packet is None:
        return _reason(UnresolvedReason.RESEARCH_MISSING)

    # V41: only the packet this merchant derives, naming this merchant, is usable.
    if (
        packet.packet_id != packet_id
        or packet.normalized_merchant != normalized_merchant
    ):
        return _reason(UnresolvedReason.RESEARCH_MALFORMED)

    if packet.is_stale():
        return _reason(UnresolvedReason.RESEARCH_STALE)

    if packet.status is PacketStatus.FAILED:
        failure_reason = packet.failure_reason
        if failure_reason is None or failure_reason not in RESEARCH_EXECUTION_REASONS:
            return _reason(UnresolvedReason.RESEARCH_MALFORMED)
        return _reason(failure_reason)

    return _resolve_approval(packet, packet_id, root=root)


def _resolve_approval(
    packet: ResearchPacket, packet_id: str, *, root: Path | None
) -> PacketResolution:
    """V47: select and suggest_new require approval bound to this exact packet."""
    try:
        record = review_record_for(packet_id, root=root)
    except ResearchPacketError:
        # A malformed review artifact approves nothing.
        return _reason(UnresolvedReason.RESEARCH_UNAPPROVED)

    if record is None or record.status is not PacketReviewStatus.APPROVED:
        return _reason(UnresolvedReason.RESEARCH_UNAPPROVED)

    # Approval binds the exact hash and both packet versions.
    if record.packet_sha256 != packet.packet_sha256:
        return _reason(UnresolvedReason.RESEARCH_UNAPPROVED)
    if (
        record.schema_version != packet.schema_version
        or record.query_version != packet.query_version
    ):
        return _reason(UnresolvedReason.RESEARCH_UNAPPROVED)

    return PacketResolution(packet=packet)

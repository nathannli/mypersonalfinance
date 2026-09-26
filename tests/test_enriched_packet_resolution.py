"""Tests for the approved-packet resolver (T8).

Pins the resolution precedence of V15, V41, V47, V55 and V57: absent is
missing, a hash mismatch is tampering, version drift is stale, a stored
execution failure keeps its exact reason, and select/suggest_new require an
approval bound to the exact packet hash.
"""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from services.enriched_categorization import (
    PacketResolution,
    resolve_approved_packet,
)
from services.research_packets import (
    RESEARCH_EXECUTION_REASONS,
    FetchedPage,
    PacketReviewRecord,
    PacketReviewStatus,
    PacketStatus,
    ResearchPacket,
    SearchResult,
    record_review,
    store_failure_packet,
    store_packet,
)
from services.transaction_categorization import UnresolvedReason

MERCHANT = "acme widgets"
EVIDENCE_URL = "https://acme.example/about"


def make_packet(merchant: str = MERCHANT, **overrides) -> ResearchPacket:
    values: dict = {
        "normalized_merchant": merchant,
        "derived_query": merchant,
        "status": PacketStatus.COMPLETE,
        "searched_at": "2026-09-17T12:00:00+00:00",
        "search_results": (
            SearchResult(
                position=0,
                site_name="Acme",
                title="Acme",
                snippet="We make widgets",
                url=EVIDENCE_URL,
            ),
        ),
        "fetched_pages": (
            FetchedPage(
                url=EVIDENCE_URL,
                final_url=EVIDENCE_URL,
                title="Acme",
                description="Widget maker",
                text="Acme makes widgets.",
                relevance_matched_tokens=("acme",),
            ),
        ),
    }
    values.update(overrides)
    return ResearchPacket(**values)


class ResolutionTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self._temporary = tempfile.TemporaryDirectory()
        self.root = Path(self._temporary.name)

    def tearDown(self) -> None:
        self._temporary.cleanup()

    def resolve(self, merchant: str = MERCHANT) -> PacketResolution:
        return resolve_approved_packet(merchant, root=self.root)

    def store_complete(self, **overrides) -> ResearchPacket:
        packet = make_packet(**overrides)
        store_packet(packet, root=self.root)
        return packet

    def approve(self, packet: ResearchPacket, **overrides) -> None:
        values: dict = {
            "packet_id": packet.packet_id,
            "packet_sha256": packet.packet_sha256,
            "status": PacketReviewStatus.APPROVED,
            "reviewed_at": "2026-09-17T12:00:00+00:00",
            "schema_version": packet.schema_version,
            "query_version": packet.query_version,
        }
        values.update(overrides)
        record_review(PacketReviewRecord(**values), root=self.root)

    def packet_path(self, packet: ResearchPacket) -> Path:
        return self.root / ".transaction-web-research" / f"{packet.packet_id}.json"

    def assert_reason(self, resolution: PacketResolution, reason) -> None:
        self.assertIsNone(resolution.packet)
        self.assertIs(resolution.reason, reason)
        self.assertFalse(resolution.approved)


class TestResolutionInvariants(unittest.TestCase):
    def test_exactly_one_of_packet_or_reason(self):
        with self.assertRaises(ValueError):
            PacketResolution()
        with self.assertRaises(ValueError):
            PacketResolution(
                packet=make_packet(), reason=UnresolvedReason.RESEARCH_MISSING
            )

    def test_approved_requires_a_packet(self):
        self.assertTrue(PacketResolution(packet=make_packet()).approved)
        self.assertFalse(
            PacketResolution(reason=UnresolvedReason.RESEARCH_MISSING).approved
        )


class TestPacketPresence(ResolutionTestCase):
    def test_absent_packet_is_research_missing(self):
        self.assert_reason(self.resolve(), UnresolvedReason.RESEARCH_MISSING)

    def test_another_merchants_packet_does_not_satisfy_this_row(self):
        store_packet(make_packet("other merchant"), root=self.root)
        self.assert_reason(self.resolve(), UnresolvedReason.RESEARCH_MISSING)

    def test_approved_complete_packet_is_usable(self):
        packet = self.store_complete()
        self.approve(packet)

        resolution = self.resolve()

        self.assertTrue(resolution.approved)
        # The resolver returns a freshly loaded packet, not the stored object.
        assert resolution.packet is not None
        self.assertEqual(resolution.packet.packet_sha256, packet.packet_sha256)
        self.assertEqual(resolution.packet.normalized_merchant, MERCHANT)


class TestPacketIntegrity(ResolutionTestCase):
    def test_edited_packet_content_is_tampering_not_stale(self):
        self.store_complete()
        path = next((self.root / ".transaction-web-research").iterdir())
        document = path.read_text(encoding="utf-8").replace(
            '"derived_query":"acme widgets"', '"derived_query":"something else"'
        )
        path.write_text(document, encoding="utf-8")

        self.assert_reason(self.resolve(), UnresolvedReason.RESEARCH_TAMPERED)

    def test_unknown_packet_field_is_malformed(self):
        packet = self.store_complete()
        path = self.packet_path(packet)
        document = json.loads(path.read_text(encoding="utf-8"))
        document["unexpected_field"] = "surprise"
        path.write_text(json.dumps(document), encoding="utf-8")

        self.assert_reason(self.resolve(), UnresolvedReason.RESEARCH_MALFORMED)

    def test_merchant_mismatch_is_malformed(self):
        # V41's defensive branch: the store normally guarantees this, so the
        # loader is stubbed to prove the resolver rejects it independently.
        mismatch = make_packet("someone else")
        with patch(
            "services.enriched_categorization.load_packet_if_present",
            return_value=mismatch,
        ):
            self.assert_reason(self.resolve(), UnresolvedReason.RESEARCH_MALFORMED)


class TestPacketStaleness(ResolutionTestCase):
    def test_version_drift_is_stale(self):
        packet = self.store_complete(schema_version="transaction-web-research-v0")
        self.approve(packet)

        self.assert_reason(self.resolve(), UnresolvedReason.RESEARCH_STALE)

    def test_query_version_drift_is_stale(self):
        packet = self.store_complete(query_version="merchant-research-v1")
        self.approve(packet)

        self.assert_reason(self.resolve(), UnresolvedReason.RESEARCH_STALE)

    def test_staleness_precedes_a_stored_failure_reason(self):
        # V57 applies to every stored packet, failed ones included.
        packet = ResearchPacket(
            normalized_merchant=MERCHANT,
            derived_query=MERCHANT,
            status=PacketStatus.FAILED,
            searched_at="2026-09-17T12:00:00+00:00",
            failure_reason=UnresolvedReason.RESEARCH_TIMEOUT,
            schema_version="transaction-web-research-v0",
        )
        store_failure_packet(packet, root=self.root)

        self.assert_reason(self.resolve(), UnresolvedReason.RESEARCH_STALE)

    def test_tampering_precedes_staleness(self):
        packet = self.store_complete(schema_version="transaction-web-research-v0")
        path = self.packet_path(packet)
        document = path.read_text(encoding="utf-8").replace(
            '"derived_query":"acme widgets"', '"derived_query":"tampered"'
        )
        path.write_text(document, encoding="utf-8")

        self.assert_reason(self.resolve(), UnresolvedReason.RESEARCH_TAMPERED)


class TestFailurePackets(ResolutionTestCase):
    def test_every_execution_failure_reason_surfaces_exactly(self):
        for reason in sorted(RESEARCH_EXECUTION_REASONS):
            with self.subTest(reason=reason.value):
                with tempfile.TemporaryDirectory() as temporary:
                    root = Path(temporary)
                    packet = ResearchPacket(
                        normalized_merchant=MERCHANT,
                        derived_query=MERCHANT,
                        status=PacketStatus.FAILED,
                        searched_at="2026-09-17T12:00:00+00:00",
                        failure_reason=reason,
                    )
                    store_failure_packet(packet, root=root)

                    resolution = resolve_approved_packet(MERCHANT, root=root)

                    self.assertEqual(resolution.reason, reason)
                    self.assertIsNone(resolution.packet)

    def test_a_failure_packet_is_never_supported_by_an_approval(self):
        packet = ResearchPacket(
            normalized_merchant=MERCHANT,
            derived_query=MERCHANT,
            status=PacketStatus.FAILED,
            searched_at="2026-09-17T12:00:00+00:00",
            failure_reason=UnresolvedReason.RESEARCH_NO_RESULTS,
        )
        store_failure_packet(packet, root=self.root)

        self.assertFalse(self.resolve().approved)


class TestApprovalGate(ResolutionTestCase):
    def test_unapproved_packet_is_research_unapproved(self):
        self.store_complete()
        self.assert_reason(self.resolve(), UnresolvedReason.RESEARCH_UNAPPROVED)

    def test_rejected_packet_is_research_unapproved(self):
        packet = self.store_complete()
        self.approve(packet, status=PacketReviewStatus.REJECTED, reason="wrong company")

        self.assert_reason(self.resolve(), UnresolvedReason.RESEARCH_UNAPPROVED)

    def test_approval_for_a_different_hash_is_research_unapproved(self):
        # V47: approval binds the exact packet hash, so a refresh invalidates it.
        packet = self.store_complete()
        self.approve(packet, packet_sha256="a" * 64)

        self.assert_reason(self.resolve(), UnresolvedReason.RESEARCH_UNAPPROVED)

    def test_approval_with_older_schema_version_is_research_unapproved(self):
        packet = self.store_complete()
        self.approve(packet, schema_version="transaction-web-research-v0")

        self.assert_reason(self.resolve(), UnresolvedReason.RESEARCH_UNAPPROVED)

    def test_approval_with_older_query_version_is_research_unapproved(self):
        packet = self.store_complete()
        self.approve(packet, query_version="merchant-research-v1")

        self.assert_reason(self.resolve(), UnresolvedReason.RESEARCH_UNAPPROVED)

    def test_malformed_approval_artifact_approves_nothing(self):
        packet = self.store_complete()
        self.approve(packet)
        review_path = self.root / ".transaction-web-research-approvals.json"
        review_path.write_text("{not json", encoding="utf-8")

        self.assert_reason(self.resolve(), UnresolvedReason.RESEARCH_UNAPPROVED)


if __name__ == "__main__":
    unittest.main()

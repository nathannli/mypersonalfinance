"""Tests for the private packet and review stores (T4)."""

from __future__ import annotations

import fcntl
import json
import os
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest import mock

from services import research_packets as rp
from services.research_packets import (
    RESEARCH_QUERY_VERSION,
    RESEARCH_SCHEMA_VERSION,
    FetchedPage,
    PacketReviewRecord,
    PacketReviewStatus,
    PacketStatus,
    ResearchPacket,
    ResearchPacketMissingError,
    ResearchPacketTamperedError,
    ResearchStoreError,
    ResearchStoreLockedError,
    SearchResult,
    load_packet,
    load_packet_if_present,
    load_review_records,
    packet_path_for,
    record_review,
    repo_root,
    review_record_for,
    review_records_path,
    store_failure_packet,
    store_packet,
)
from services.transaction_categorization import UnresolvedReason
from utils.repo_paths import (
    PRIVATE_RESEARCH_DIRNAME,
    RESEARCH_LOCK_FILENAME,
    RESEARCH_REVIEW_FILENAME,
    SUGGESTION_FILENAME,
)

SEARCH_URL = "https://acme.example/about"
REVIEWED_AT = "2026-09-17T13:00:00+00:00"


def make_packet(
    merchant: str = "acme widgets",
    *,
    searched_at: str = "2026-09-17T12:00:00+00:00",
    schema_version: str = RESEARCH_SCHEMA_VERSION,
    query_version: str = RESEARCH_QUERY_VERSION,
) -> ResearchPacket:
    return ResearchPacket(
        normalized_merchant=merchant,
        derived_query="acme widgets",
        status=PacketStatus.COMPLETE,
        searched_at=searched_at,
        schema_version=schema_version,
        query_version=query_version,
        search_results=(
            SearchResult(
                position=0,
                site_name="Acme",
                title="Acme Widgets",
                snippet="We make widgets",
                url=SEARCH_URL,
            ),
        ),
        fetched_pages=(
            FetchedPage(
                url=SEARCH_URL,
                final_url=SEARCH_URL,
                title="Acme Widgets",
                description="A widget maker",
                text="Acme makes widgets in Toronto.",
                relevance_matched_tokens=("acme", "widgets"),
            ),
        ),
    )


def make_failure(
    merchant: str = "acme widgets",
    reason: UnresolvedReason = UnresolvedReason.RESEARCH_TIMEOUT,
) -> ResearchPacket:
    return ResearchPacket(
        normalized_merchant=merchant,
        derived_query="acme widgets",
        status=PacketStatus.FAILED,
        searched_at="2026-09-17T12:00:00+00:00",
        failure_reason=reason,
    )


def make_record(
    packet: ResearchPacket,
    *,
    status: PacketReviewStatus = PacketReviewStatus.APPROVED,
    reason: str | None = None,
) -> PacketReviewRecord:
    return PacketReviewRecord(
        packet_id=packet.packet_id,
        packet_sha256=packet.packet_sha256,
        status=status,
        reviewed_at=REVIEWED_AT,
        reason=reason,
    )


class StoreTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self._temporary = tempfile.TemporaryDirectory()
        self.root = Path(self._temporary.name)

    def tearDown(self) -> None:
        self._temporary.cleanup()

    def leftovers(self, directory: Path) -> list[str]:
        return sorted(p.name for p in directory.iterdir() if p.name.endswith(".tmp"))


class TestPrivateRoot(StoreTestCase):
    def test_paths_derive_from_the_repository_root_not_the_cwd(self):
        expected_root = Path(rp.__file__).resolve().parents[1]
        self.assertEqual(repo_root(), expected_root)

        previous = os.getcwd()
        os.chdir(self.root)
        try:
            self.assertEqual(repo_root(), expected_root)
            self.assertEqual(
                packet_path_for("a" * 64).parent,
                expected_root / PRIVATE_RESEARCH_DIRNAME,
            )
            self.assertEqual(
                review_records_path(), expected_root / RESEARCH_REVIEW_FILENAME
            )
        finally:
            os.chdir(previous)

    def test_ignore_rules_exist_for_every_private_artifact(self):
        # V11: the ignore rules must exist before any artifact is created.
        gitignore = (repo_root() / ".gitignore").read_text(encoding="utf-8")
        for entry in (
            f"{PRIVATE_RESEARCH_DIRNAME}/",
            RESEARCH_REVIEW_FILENAME,
            RESEARCH_LOCK_FILENAME,
            SUGGESTION_FILENAME,
        ):
            with self.subTest(entry=entry):
                self.assertIn(entry, gitignore)

    def test_packet_filename_contains_only_the_packet_id(self):
        path = packet_path_for("b" * 64, root=self.root)
        self.assertEqual(path.name, f"{'b' * 64}.json")
        self.assertEqual(path.parent, self.root / PRIVATE_RESEARCH_DIRNAME)

    def test_rejects_a_packet_id_that_is_not_a_digest(self):
        # A non-digest could otherwise carry a separator and escape the store.
        for bad in ("", "abc", "../escape", "a" * 63, "a" * 65, f"{'a' * 63}/x", None):
            with self.subTest(bad=bad):
                with self.assertRaises(ResearchStoreError):
                    packet_path_for(bad)  # type: ignore[arg-type]

    def test_one_packet_file_per_normalized_merchant(self):
        # V8: at most one active frozen packet per normalized merchant.
        first = packet_path_for(make_packet().packet_id, root=self.root)
        second = packet_path_for(make_packet().packet_id, root=self.root)
        other = packet_path_for(make_packet("other merchant").packet_id, root=self.root)
        self.assertEqual(first, second)
        self.assertNotEqual(first, other)


class TestPacketStore(StoreTestCase):
    def test_store_then_load_round_trips_canonical_bytes(self):
        packet = make_packet()
        path = store_packet(packet, root=self.root)

        self.assertEqual(json.loads(path.read_bytes()), packet.as_dict())
        loaded = load_packet(packet.packet_id, root=self.root)
        self.assertEqual(loaded.as_dict(), packet.as_dict())
        self.assertEqual(loaded.packet_sha256, packet.packet_sha256)
        self.assertFalse(loaded.is_stale())
        self.assertEqual(self.leftovers(path.parent), [])

    def test_missing_packet_is_typed_and_optional_lookup_is_none(self):
        absent = "c" * 64
        with self.assertRaises(ResearchPacketMissingError):
            load_packet(absent, root=self.root)
        self.assertIsNone(load_packet_if_present(absent, root=self.root))

    def test_non_json_packet_is_malformed(self):
        packet = make_packet()
        path = packet_path_for(packet.packet_id, root=self.root)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"not json at all")
        with self.assertRaises(rp.ResearchPacketMalformedError):
            load_packet(packet.packet_id, root=self.root)

    def test_version_drift_is_stale_while_hash_disagreement_is_tampering(self):
        drifted = make_packet(schema_version="transaction-web-research-v0")
        path = store_packet(drifted, root=self.root)
        self.assertTrue(load_packet(drifted.packet_id, root=self.root).is_stale())

        document = json.loads(path.read_bytes())
        document["derived_query"] = "something else entirely"
        path.write_bytes(json.dumps(document).encode())
        # V57: a hash disagreement is tampering, never staleness.
        with self.assertRaises(ResearchPacketTamperedError):
            load_packet(drifted.packet_id, root=self.root)

    def test_storing_a_complete_packet_twice_refreshes_it(self):
        packet = make_packet()
        store_packet(packet, root=self.root)
        refreshed = replace(packet, searched_at="2026-09-17T15:00:00+00:00")
        store_packet(refreshed, root=self.root)

        loaded = load_packet(packet.packet_id, root=self.root)
        self.assertEqual(loaded.packet_id, packet.packet_id)
        self.assertEqual(loaded.packet_sha256, refreshed.packet_sha256)
        self.assertNotEqual(loaded.packet_sha256, packet.packet_sha256)

    def test_failure_packets_use_their_own_entry_point(self):
        with self.assertRaises(ResearchStoreError):
            store_packet(make_failure(), root=self.root)
        with self.assertRaises(ResearchStoreError):
            store_failure_packet(make_packet(), root=self.root)

    def test_failure_packet_is_written_when_no_evidence_exists(self):
        written = store_failure_packet(make_failure(), root=self.root)
        self.assertIsNotNone(written)
        loaded = load_packet(make_failure().packet_id, root=self.root)
        self.assertEqual(loaded.status, PacketStatus.FAILED)
        self.assertEqual(loaded.failure_reason, UnresolvedReason.RESEARCH_TIMEOUT)
        self.assertEqual(loaded.fetched_pages, ())

    def test_failed_refresh_preserves_evidence_and_review_record(self):
        packet = make_packet()
        path = store_packet(packet, root=self.root)
        record_review(make_record(packet), root=self.root)
        before = path.read_bytes()

        # V60: a failure reports itself without destroying frozen evidence.
        self.assertIsNone(store_failure_packet(make_failure(), root=self.root))
        self.assertEqual(path.read_bytes(), before)
        self.assertIsNotNone(review_record_for(packet.packet_id, root=self.root))
        self.assertEqual(
            load_packet(packet.packet_id, root=self.root).packet_sha256,
            packet.packet_sha256,
        )

    def test_successful_refresh_returns_the_packet_to_pending_review(self):
        packet = make_packet()
        store_packet(packet, root=self.root)
        record_review(make_record(packet), root=self.root)
        self.assertIsNotNone(review_record_for(packet.packet_id, root=self.root))

        refreshed = replace(packet, searched_at="2026-09-17T16:00:00+00:00")
        store_packet(refreshed, root=self.root)
        # V52: the old approval bound a superseded hash, so it no longer applies.
        self.assertIsNone(review_record_for(refreshed.packet_id, root=self.root))

    def test_failed_replace_keeps_the_previous_file_and_no_temp_file(self):
        packet = make_packet()
        path = store_packet(packet, root=self.root)
        before = path.read_bytes()

        with mock.patch(
            "services.research_packets.os.replace", side_effect=OSError("disk full")
        ):
            with self.assertRaises(OSError):
                store_packet(
                    replace(packet, searched_at="2026-09-17T17:00:00+00:00"),
                    root=self.root,
                )

        self.assertEqual(path.read_bytes(), before)
        self.assertEqual(self.leftovers(path.parent), [])

    def test_competing_writer_fails_fast_without_mutation(self):
        packet = make_packet()
        lock_path = self.root / RESEARCH_LOCK_FILENAME
        descriptor = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o600)
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
            # V50: the second writer does not queue and does not mutate.
            with self.assertRaises(ResearchStoreLockedError):
                store_packet(packet, root=self.root)
        finally:
            os.close(descriptor)

        self.assertFalse(packet_path_for(packet.packet_id, root=self.root).exists())
        # The lock is advisory and released with the descriptor.
        self.assertIsNotNone(store_packet(packet, root=self.root))

    def test_packet_file_carries_no_extra_fields(self):
        # V34: the frozen artifact contains only canonical packet fields.
        packet = make_packet()
        path = store_packet(packet, root=self.root)
        self.assertEqual(
            set(json.loads(path.read_bytes())), ResearchPacket.PACKET_FIELDS
        )


class TestReviewStore(StoreTestCase):
    def test_absent_artifact_reads_as_empty(self):
        self.assertEqual(load_review_records(root=self.root), {})
        self.assertIsNone(review_record_for("d" * 64, root=self.root))

    def test_approval_round_trips_and_is_looked_up_by_packet_id(self):
        packet = make_packet()
        store_packet(packet, root=self.root)
        record_review(make_record(packet), root=self.root)

        stored = review_record_for(packet.packet_id, root=self.root)
        assert stored is not None
        self.assertEqual(stored.status, PacketReviewStatus.APPROVED)
        self.assertEqual(stored.packet_sha256, packet.packet_sha256)
        self.assertIsNone(stored.reason)
        self.assertEqual(self.leftovers(self.root), [])

    def test_rejection_requires_a_bounded_nonblank_reason(self):
        packet = make_packet()
        store_packet(packet, root=self.root)
        record_review(
            make_record(
                packet,
                status=PacketReviewStatus.REJECTED,
                reason="Evidence is about a different company.",
            ),
            root=self.root,
        )
        stored = review_record_for(packet.packet_id, root=self.root)
        assert stored is not None
        self.assertEqual(stored.status, PacketReviewStatus.REJECTED)
        self.assertIn("different company", stored.reason or "")

        for blank in (None, "", "   "):
            with self.subTest(blank=blank):
                with self.assertRaises(rp.ResearchPacketError):
                    make_record(
                        packet, status=PacketReviewStatus.REJECTED, reason=blank
                    )
        with self.assertRaises(rp.ResearchPacketError):
            make_record(
                packet,
                status=PacketReviewStatus.REJECTED,
                reason="x" * (rp.MAX_REJECTION_REASON_CHARS + 1),
            )

    def test_approval_may_not_carry_a_rejection_reason(self):
        packet = make_packet()
        with self.assertRaises(rp.ResearchPacketError):
            make_record(packet, reason="not applicable")

    def test_a_decided_record_cannot_be_pending(self):
        packet = make_packet()
        with self.assertRaises(rp.ResearchPacketError):
            make_record(packet, status=PacketReviewStatus.PENDING)

    def test_later_decision_supersedes_the_earlier_one(self):
        packet = make_packet()
        store_packet(packet, root=self.root)
        record_review(
            make_record(
                packet, status=PacketReviewStatus.REJECTED, reason="Looked wrong."
            ),
            root=self.root,
        )
        record_review(make_record(packet), root=self.root)
        stored = review_record_for(packet.packet_id, root=self.root)
        assert stored is not None
        self.assertEqual(stored.status, PacketReviewStatus.APPROVED)

    def test_artifact_holds_no_page_content_or_merchant_text(self):
        packet = make_packet()
        store_packet(packet, root=self.root)
        record_review(make_record(packet), root=self.root)

        raw = review_records_path(root=self.root).read_text(encoding="utf-8")
        self.assertNotIn("acme widgets", raw)
        self.assertNotIn("Acme makes widgets", raw)
        for value in json.loads(raw).values():
            self.assertEqual(set(value), PacketReviewRecord.RECORD_FIELDS)

    def test_malformed_artifacts_are_rejected(self):
        packet = make_packet()
        good = make_record(packet).as_dict()
        cases = (
            ("not json", b"nope"),
            ("top-level list", json.dumps([1]).encode()),
            ("record not an object", json.dumps({packet.packet_id: 5}).encode()),
            (
                "key does not match packet_id",
                json.dumps({"e" * 64: good}).encode(),
            ),
            (
                "unknown status",
                json.dumps({packet.packet_id: {**good, "status": "maybe"}}).encode(),
            ),
        )
        for name, body in cases:
            with self.subTest(name=name):
                path = review_records_path(root=self.root)
                path.write_bytes(body)
                with self.assertRaises(rp.ResearchPacketError):
                    load_review_records(root=self.root)


if __name__ == "__main__":
    unittest.main()


class TestRefreshWriteOrdering(StoreTestCase):
    """The superseded record is discarded before the packet is written (V52).

    The two writes are independent, so a crash between them must not leave a
    fresh packet carrying a record that still approves the old hash.
    """

    def test_failed_packet_write_leaves_no_record_and_keeps_old_evidence(self):
        original = make_packet()
        store_packet(original, root=self.root)
        record_review(make_record(original), root=self.root)

        refreshed = make_packet(searched_at="2026-09-18T12:00:00+00:00")
        self.assertNotEqual(refreshed.packet_sha256, original.packet_sha256)

        packet_path = packet_path_for(original.packet_id, root=self.root)
        real_atomic_write = rp._atomic_write

        def fail_only_on_the_packet(path, payload):
            if path == packet_path:
                raise OSError("simulated crash before the packet replace")
            return real_atomic_write(path, payload)

        with mock.patch.object(
            rp, "_atomic_write", side_effect=fail_only_on_the_packet
        ):
            with self.assertRaises(OSError):
                store_packet(refreshed, root=self.root)

        # The stale approval is gone, so the surviving packet reads as pending
        # and the load path fails closed on it.
        self.assertIsNone(review_record_for(original.packet_id, root=self.root))
        stored = load_packet(original.packet_id, root=self.root)
        self.assertEqual(stored.packet_sha256, original.packet_sha256)
        self.assertEqual(self.leftovers(packet_path.parent), [])

    def test_a_successful_refresh_discards_the_superseded_record(self):
        original = make_packet()
        store_packet(original, root=self.root)
        record_review(make_record(original), root=self.root)

        refreshed = make_packet(searched_at="2026-09-18T12:00:00+00:00")
        store_packet(refreshed, root=self.root)

        self.assertIsNone(review_record_for(original.packet_id, root=self.root))
        self.assertEqual(
            load_packet(original.packet_id, root=self.root).packet_sha256,
            refreshed.packet_sha256,
        )

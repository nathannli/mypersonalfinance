"""Tests for the private grouped suggestion artifact (T9).

Pins V22/V24: suggestions group by normalized merchant, serialize canonically,
collapse identical proposals onto one record while unioning the affected
context fingerprints, and never report a persist that failed.
"""

from __future__ import annotations

import json
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from services.research_packets import (
    SUGGESTION_SCHEMA_VERSION,
    CategorySuggestion,
    ResearchPacketError,
    ResearchPacketMalformedError,
    ResearchStoreError,
    ResearchStoreLockedError,
    SuggestionArtifact,
    load_suggestions,
    record_suggestion,
    research_writer_lock,
    suggestion_records_path,
)

URL_ONE = "https://acme.example/about"
URL_TWO = "https://acme.example/pricing"
PACKET_HASH = "a" * 64
FINGERPRINT_ONE = "1" * 64
FINGERPRINT_TWO = "2" * 64
FINGERPRINT_THREE = "3" * 64


def make_suggestion(**overrides: object) -> CategorySuggestion:
    defaults: dict[str, object] = {
        "normalized_merchant": "acme widgets",
        "category_name": "Gadgets",
        "subcategory_name": "Widgets",
        "rationale": "Sells widgets",
        "evidence_urls": (URL_ONE,),
        "research_packet_sha256": PACKET_HASH,
        "context_fingerprints": (FINGERPRINT_ONE,),
    }
    defaults.update(overrides)
    return CategorySuggestion(**defaults)  # type: ignore[arg-type]


class SuggestionArtifactTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self._temporary = TemporaryDirectory()
        self.addCleanup(self._temporary.cleanup)
        self.root = Path(self._temporary.name)

    def read_payload(self) -> dict:
        return json.loads(suggestion_records_path(root=self.root).read_text("utf-8"))

    def write_payload(self, payload: object) -> None:
        suggestion_records_path(root=self.root).write_text(
            json.dumps(payload), encoding="utf-8"
        )


class TestSuggestionGrouping(SuggestionArtifactTestCase):
    def test_recording_groups_by_normalized_merchant(self):
        record_suggestion(make_suggestion(), root=self.root)
        record_suggestion(
            make_suggestion(normalized_merchant="beta mart"), root=self.root
        )

        grouped = load_suggestions(root=self.root)

        self.assertEqual(sorted(grouped), ["acme widgets", "beta mart"])
        self.assertEqual(len(grouped["acme widgets"]), 1)

    def test_distinct_proposals_for_one_merchant_stay_separate(self):
        first = record_suggestion(make_suggestion(), root=self.root)
        second = record_suggestion(
            make_suggestion(subcategory_name="Gizmos"), root=self.root
        )

        stored = load_suggestions(root=self.root)["acme widgets"]

        self.assertEqual(len(stored), 2)
        self.assertNotEqual(first.suggestion_id, second.suggestion_id)
        # Canonical ordering: each group is sorted by suggestion id.
        self.assertEqual(
            [item.suggestion_id for item in stored],
            sorted([first.suggestion_id, second.suggestion_id]),
        )

    def test_parent_category_id_is_part_of_the_proposal_identity(self):
        without_parent = record_suggestion(make_suggestion(), root=self.root)
        with_parent = record_suggestion(
            make_suggestion(parent_category_id=3), root=self.root
        )

        # V24: the proposed taxonomy includes the parent, so these differ.
        self.assertNotEqual(without_parent.suggestion_id, with_parent.suggestion_id)
        self.assertEqual(len(load_suggestions(root=self.root)["acme widgets"]), 2)

    def test_records_path_is_root_stable(self):
        self.assertEqual(
            suggestion_records_path(root=self.root),
            self.root / ".transaction-category-suggestions.json",
        )


class TestSuggestionDeduplication(SuggestionArtifactTestCase):
    def test_repeated_identical_proposal_unions_fingerprints(self):
        first = record_suggestion(make_suggestion(), root=self.root)
        second = record_suggestion(
            make_suggestion(context_fingerprints=(FINGERPRINT_TWO, FINGERPRINT_ONE)),
            root=self.root,
        )

        stored = load_suggestions(root=self.root)["acme widgets"]

        self.assertEqual(len(stored), 1)
        self.assertEqual(first.suggestion_id, second.suggestion_id)
        # Sorted and unique, so repeated rows stay traceable without growth.
        self.assertEqual(
            stored[0].context_fingerprints,
            (FINGERPRINT_ONE, FINGERPRINT_TWO),
        )

    def test_identical_proposal_keeps_the_first_rationale_and_citations(self):
        record_suggestion(make_suggestion(evidence_urls=(URL_ONE,)), root=self.root)
        record_suggestion(
            make_suggestion(
                rationale="A later, different rationale",
                evidence_urls=(URL_TWO,),
            ),
            root=self.root,
        )

        stored = load_suggestions(root=self.root)["acme widgets"][0]

        self.assertEqual(stored.rationale, "Sells widgets")
        self.assertEqual(stored.evidence_urls, (URL_ONE,))

    def test_merge_is_order_independent_and_canonical(self):
        first = make_suggestion(subcategory_name="Gizmos")
        second = make_suggestion()
        record_suggestion(first, root=self.root)
        record_suggestion(second, root=self.root)
        forward = suggestion_records_path(root=self.root).read_bytes()

        other_root = Path(TemporaryDirectory().name)
        self.addCleanup(lambda: __import__("shutil").rmtree(other_root, True))
        record_suggestion(second, root=other_root)
        record_suggestion(first, root=other_root)

        self.assertEqual(forward, suggestion_records_path(root=other_root).read_bytes())

    def test_identical_content_rewrites_identical_bytes(self):
        suggestion = make_suggestion()
        record_suggestion(suggestion, root=self.root)
        first = suggestion_records_path(root=self.root).read_bytes()

        record_suggestion(suggestion, root=self.root)

        self.assertEqual(suggestion_records_path(root=self.root).read_bytes(), first)


class TestSuggestionArtifactValidation(SuggestionArtifactTestCase):
    def test_absent_artifact_loads_as_empty(self):
        self.assertEqual(load_suggestions(root=self.root), {})

    def test_payload_is_versioned_and_grouped(self):
        record_suggestion(make_suggestion(), root=self.root)

        payload = self.read_payload()

        self.assertEqual(payload["schema_version"], SUGGESTION_SCHEMA_VERSION)
        self.assertEqual(list(payload["merchants"]), ["acme widgets"])
        self.assertEqual(
            payload["merchants"]["acme widgets"][0]["normalized_merchant"],
            "acme widgets",
        )

    def test_unsupported_schema_version_is_rejected(self):
        with self.assertRaises(ResearchPacketMalformedError):
            SuggestionArtifact(merchants={}, schema_version="other-version")

        self.write_payload({"schema_version": "other-version", "merchants": {}})
        with self.assertRaises(ResearchPacketMalformedError):
            load_suggestions(root=self.root)

    def test_non_json_artifact_is_rejected(self):
        suggestion_records_path(root=self.root).write_text(
            "{not json", encoding="utf-8"
        )

        with self.assertRaises(ResearchPacketMalformedError):
            load_suggestions(root=self.root)

    def test_extra_top_level_keys_are_rejected(self):
        self.write_payload(
            {"schema_version": SUGGESTION_SCHEMA_VERSION, "merchants": {}, "extra": 1}
        )

        with self.assertRaises(ResearchPacketMalformedError):
            load_suggestions(root=self.root)

    def test_unnormalized_group_key_is_rejected(self):
        self.write_payload(
            {
                "schema_version": SUGGESTION_SCHEMA_VERSION,
                "merchants": {"Acme  Widgets": []},
            }
        )

        with self.assertRaises(ResearchPacketMalformedError):
            load_suggestions(root=self.root)

    def test_suggestion_outside_its_group_is_rejected(self):
        self.write_payload(
            {
                "schema_version": SUGGESTION_SCHEMA_VERSION,
                "merchants": {"beta mart": [make_suggestion().as_dict()]},
            }
        )

        with self.assertRaises(ResearchPacketMalformedError):
            load_suggestions(root=self.root)

    def test_duplicate_suggestion_ids_are_rejected(self):
        entry = make_suggestion().as_dict()
        self.write_payload(
            {
                "schema_version": SUGGESTION_SCHEMA_VERSION,
                "merchants": {"acme widgets": [entry, dict(entry)]},
            }
        )

        with self.assertRaises(ResearchPacketMalformedError):
            load_suggestions(root=self.root)

    def test_record_requires_a_category_suggestion(self):
        with self.assertRaises(ResearchStoreError):
            record_suggestion("not a suggestion", root=self.root)  # type: ignore[arg-type]

    def test_fingerprints_must_be_sha256_digests_and_are_canonicalized(self):
        with self.assertRaises(ResearchPacketError):
            make_suggestion(context_fingerprints=("short",))

        canonical = make_suggestion(
            context_fingerprints=(FINGERPRINT_TWO, FINGERPRINT_ONE, FINGERPRINT_TWO)
        )

        self.assertEqual(
            canonical.context_fingerprints, (FINGERPRINT_ONE, FINGERPRINT_TWO)
        )

    def test_round_trip_preserves_every_stored_field(self):
        original = record_suggestion(
            make_suggestion(
                parent_category_id=3,
                context_fingerprints=(FINGERPRINT_THREE, FINGERPRINT_ONE),
            ),
            root=self.root,
        )

        stored = load_suggestions(root=self.root)["acme widgets"][0]

        self.assertEqual(stored, original)
        self.assertEqual(stored.as_dict(), original.as_dict())


class TestSuggestionWriterLock(SuggestionArtifactTestCase):
    def test_lock_contention_is_reported_and_mutates_nothing(self):
        record_suggestion(make_suggestion(), root=self.root)
        before = suggestion_records_path(root=self.root).read_bytes()

        with research_writer_lock(root=self.root):
            with self.assertRaises(ResearchStoreLockedError):
                record_suggestion(
                    make_suggestion(subcategory_name="Gizmos"), root=self.root
                )

        # V22: a failed persist leaves the artifact exactly as it was.
        self.assertEqual(suggestion_records_path(root=self.root).read_bytes(), before)
        self.assertEqual(len(load_suggestions(root=self.root)["acme widgets"]), 1)

    def test_lock_is_released_after_a_successful_write(self):
        record_suggestion(make_suggestion(), root=self.root)

        # A second writer succeeds because the lock is not left behind.
        record_suggestion(make_suggestion(subcategory_name="Gizmos"), root=self.root)

        self.assertEqual(len(load_suggestions(root=self.root)["acme widgets"]), 2)


if __name__ == "__main__":
    unittest.main()

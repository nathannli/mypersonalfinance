"""Tests for the canonical research-packet, review, and suggestion types (T1)."""

from __future__ import annotations

import hashlib
import unittest

from services.research_packets import (
    DEFAULT_RESEARCH_PURPOSE,
    FIXED_RESEARCH_LANGUAGE,
    FIXED_RESEARCH_LOCATION,
    MAX_EVIDENCE_URLS,
    MAX_FETCH_CHARS_PER_PAGE,
    MAX_FETCH_CHARS_TOTAL,
    MAX_FETCH_URLS,
    MAX_SEARCH_RESULTS,
    MAX_SUGGESTION_NAME_CHARS,
    MAX_SUGGESTION_RATIONALE_CHARS,
    RESEARCH_EXECUTION_REASONS,
    RESEARCH_PACKET_ID_PREFIX,
    RESEARCH_QUERY_VERSION,
    RESEARCH_SCHEMA_VERSION,
    RESEARCH_STATE_REASONS,
    SUGGESTION_ID_LENGTH,
    CategorySuggestion,
    FetchedPage,
    PacketReviewRecord,
    PacketReviewStatus,
    PacketStatus,
    ResearchPacket,
    ResearchPacketError,
    ResearchPacketMalformedError,
    ResearchPacketTamperedError,
    ResearchRunStatus,
    SearchResult,
    packet_id_for,
)
from services.tinyfish_research import (
    ResearchAuthError,
    ResearchCircuitOpenError,
    ResearchConfigError,
    ResearchEmptyEvidenceError,
    ResearchFetchFailedError,
    ResearchIrrelevantError,
    ResearchMalformedError,
    ResearchNoResultsError,
    ResearchNoValidUrlsError,
    ResearchProviderError,
    ResearchRateLimitError,
    ResearchTimeoutError,
    TinyFishResearchConfig,
)
from services.transaction_categorization import (
    ProviderAction,
    TransactionOutcome,
    TransactionStatus,
    UnresolvedReason,
    normalize_context_text,
)

SEARCHED_AT = "2026-09-17T12:00:00+00:00"
REVIEWED_AT = "2026-09-17T13:00:00+00:00"
PACKET_HASH = "a" * 64
URL_ONE = "https://example.com/acme"
URL_TWO = "https://example.com/acme-about"
URL_THREE = "https://example.com/acme-jobs"
URL_FOUR = "https://example.com/acme-blog"


def make_result(position: int, url: str) -> SearchResult:
    return SearchResult(
        position=position,
        site_name="example.com",
        title="Acme",
        snippet="widgets",
        url=url,
    )


def make_page(
    url: str, text: str = "Acme makes widgets", tokens=("acme",)
) -> FetchedPage:
    return FetchedPage(
        url=url,
        final_url=url,
        title="Acme",
        description="widget maker",
        text=text,
        relevance_matched_tokens=tokens,
    )


def make_packet(**overrides: object) -> ResearchPacket:
    defaults: dict[str, object] = {
        "normalized_merchant": "acme",
        "derived_query": "acme",
        "status": PacketStatus.COMPLETE,
        "searched_at": SEARCHED_AT,
        "search_results": (make_result(0, URL_ONE), make_result(1, URL_TWO)),
        "fetched_pages": (make_page(URL_ONE),),
    }
    defaults.update(overrides)
    return ResearchPacket(**defaults)  # type: ignore[arg-type]


def make_suggestion(**overrides: object) -> CategorySuggestion:
    defaults: dict[str, object] = {
        "normalized_merchant": "acme",
        "category_name": "Hobbies",
        "subcategory_name": "Widgets",
        "rationale": "Sells hobby widgets",
        "evidence_urls": (URL_ONE,),
        "research_packet_sha256": PACKET_HASH,
    }
    defaults.update(overrides)
    return CategorySuggestion(**defaults)  # type: ignore[arg-type]


class TestReasonAndStatusTypes(unittest.TestCase):
    def test_every_research_reason_exists_with_exact_value(self):
        expected = {
            "research_auth",
            "research_rate_limit",
            "research_timeout",
            "research_provider_error",
            "research_no_results",
            "research_no_valid_urls",
            "research_fetch_failed",
            "research_empty_evidence",
            "research_irrelevant",
            "research_malformed",
            "research_missing",
            "research_stale",
            "research_tampered",
            "research_unapproved",
        }
        actual = {
            member.value
            for member in UnresolvedReason
            if member.value.startswith("research_")
        }
        self.assertEqual(actual, expected)

    def test_execution_and_state_reasons_partition_every_research_reason(self):
        research = {
            member
            for member in UnresolvedReason
            if member.value.startswith("research_")
        }
        self.assertEqual(RESEARCH_EXECUTION_REASONS | RESEARCH_STATE_REASONS, research)
        self.assertFalse(RESEARCH_EXECUTION_REASONS & RESEARCH_STATE_REASONS)
        for banned in (
            UnresolvedReason.RESEARCH_MISSING,
            UnresolvedReason.RESEARCH_STALE,
            UnresolvedReason.RESEARCH_TAMPERED,
            UnresolvedReason.RESEARCH_UNAPPROVED,
        ):
            self.assertNotIn(banned, RESEARCH_EXECUTION_REASONS)
            self.assertIn(banned, RESEARCH_STATE_REASONS)

    def test_suggested_status_and_suggest_new_action_exist(self):
        self.assertEqual(TransactionStatus.SUGGESTED.value, "suggested")
        self.assertEqual(ProviderAction.SUGGEST_NEW.value, "suggest_new")

    def test_transaction_outcome_carries_optional_suggestion_id(self):
        self.assertIsNone(TransactionOutcome(TransactionStatus.INSERTED).suggestion_id)
        outcome = TransactionOutcome(
            TransactionStatus.SUGGESTED, suggestion_id="abc123"
        )
        self.assertEqual(outcome.suggestion_id, "abc123")

    def test_research_run_status_values(self):
        self.assertEqual(
            {member.value for member in ResearchRunStatus},
            {"complete", "partial", "failed"},
        )


class TestPacketIdentity(unittest.TestCase):
    def test_packet_id_is_stable_sha256_of_prefixed_merchant(self):
        expected = hashlib.sha256(
            f"{RESEARCH_PACKET_ID_PREFIX}acme".encode("utf-8")
        ).hexdigest()
        self.assertEqual(packet_id_for("acme"), expected)
        self.assertEqual(make_packet().packet_id, expected)

    def test_packet_id_is_stable_across_refresh_and_differs_per_merchant(self):
        first = make_packet()
        refreshed = make_packet(
            search_results=(make_result(0, URL_THREE),),
            fetched_pages=(make_page(URL_THREE, "Acme widgets v2"),),
        )
        self.assertEqual(first.packet_id, refreshed.packet_id)
        self.assertNotEqual(first.packet_id, packet_id_for("other"))

    def test_packet_id_ignores_evidence_but_content_hash_does_not(self):
        first = make_packet()
        refreshed = make_packet(
            fetched_pages=(make_page(URL_ONE, "Acme makes gadgets"),)
        )
        self.assertEqual(first.packet_id, refreshed.packet_id)
        self.assertNotEqual(first.packet_sha256, refreshed.packet_sha256)

    def test_content_hash_is_deterministic_and_excludes_its_own_field(self):
        packet = make_packet()
        self.assertEqual(packet.packet_sha256, make_packet().packet_sha256)
        self.assertNotIn("packet_sha256", packet.as_payload())
        self.assertEqual(
            packet.packet_sha256,
            hashlib.sha256(packet.to_bytes()).hexdigest(),
        )

    def test_non_normalized_merchant_is_rejected(self):
        with self.assertRaises(ResearchPacketError):
            make_packet(normalized_merchant="Acme  Inc")
        self.assertEqual(normalize_context_text("Acme  Inc"), "acme inc")


class TestPacketFailureContract(unittest.TestCase):
    def test_failed_packet_accepts_each_execution_reason(self):
        for reason in sorted(RESEARCH_EXECUTION_REASONS, key=str):
            with self.subTest(reason=reason):
                packet = make_packet(
                    status=PacketStatus.FAILED,
                    failure_reason=reason,
                    search_results=(),
                    fetched_pages=(),
                )
                self.assertIs(packet.status, PacketStatus.FAILED)
                self.assertEqual(packet.failure_reason, reason)

    def test_failed_packet_rejects_load_or_review_reasons(self):
        for reason in sorted(RESEARCH_STATE_REASONS, key=str):
            with self.subTest(reason=reason):
                with self.assertRaises(ResearchPacketError):
                    make_packet(
                        status=PacketStatus.FAILED,
                        failure_reason=reason,
                        search_results=(),
                        fetched_pages=(),
                    )

    def test_failed_packet_requires_a_reason_and_carries_no_evidence(self):
        with self.assertRaises(ResearchPacketError):
            make_packet(
                status=PacketStatus.FAILED,
                failure_reason=None,
                search_results=(),
                fetched_pages=(),
            )
        with self.assertRaises(ResearchPacketError):
            make_packet(status=PacketStatus.FAILED, failure_reason=None)

    def test_complete_packet_rejects_failure_reason_and_needs_evidence(self):
        with self.assertRaises(ResearchPacketError):
            make_packet(failure_reason=UnresolvedReason.RESEARCH_MALFORMED)
        with self.assertRaises(ResearchPacketError):
            make_packet(fetched_pages=())
        with self.assertRaises(ResearchPacketError):
            make_packet(derived_query="")

    def test_packet_rejects_unfixed_purpose_and_locale(self):
        with self.assertRaises(ResearchPacketError):
            make_packet(purpose="identify this merchant please")
        with self.assertRaises(ResearchPacketError):
            make_packet(location="US")
        with self.assertRaises(ResearchPacketError):
            make_packet(language="fr")
        packet = make_packet()
        self.assertEqual(packet.purpose, DEFAULT_RESEARCH_PURPOSE)
        self.assertEqual(packet.location, FIXED_RESEARCH_LOCATION)
        self.assertEqual(packet.language, FIXED_RESEARCH_LANGUAGE)

    def test_packet_rejects_non_utc_timestamp(self):
        with self.assertRaises(ResearchPacketError):
            make_packet(searched_at="2026-09-17T12:00:00")
        with self.assertRaises(ResearchPacketError):
            make_packet(searched_at="not-a-timestamp")


class TestPacketBounds(unittest.TestCase):
    def test_rejects_more_than_five_search_results(self):
        results = tuple(
            make_result(index, f"https://example.com/p{index}")
            for index in range(MAX_SEARCH_RESULTS + 1)
        )
        with self.assertRaises(ResearchPacketError):
            make_packet(search_results=results)

    def test_rejects_more_than_three_fetched_pages(self):
        urls = [URL_ONE, URL_TWO, URL_THREE, URL_FOUR]
        results = tuple(make_result(index, url) for index, url in enumerate(urls))
        pages = tuple(make_page(url) for url in urls)
        with self.assertRaises(ResearchPacketError):
            make_packet(search_results=results, fetched_pages=pages)

    def test_rejects_oversized_page_text(self):
        oversized = "x" * (MAX_FETCH_CHARS_PER_PAGE + 1)
        with self.assertRaises(ResearchPacketError):
            make_packet(fetched_pages=(make_page(URL_ONE, oversized),))
        allowed = "x" * MAX_FETCH_CHARS_PER_PAGE
        packet = make_packet(fetched_pages=(make_page(URL_ONE, allowed),))
        self.assertEqual(len(packet.fetched_pages[0].text), MAX_FETCH_CHARS_PER_PAGE)

    def test_total_character_bound_is_implied_by_page_and_count_bounds(self):
        # V9 states both bounds; with three pages the arithmetic coincides, so
        # the total cap is a defence-in-depth guard rather than a separate limit.
        self.assertEqual(
            MAX_FETCH_CHARS_TOTAL, MAX_FETCH_URLS * MAX_FETCH_CHARS_PER_PAGE
        )

    def test_rejects_pages_outside_search_results_and_duplicates(self):
        with self.assertRaises(ResearchPacketError):
            make_packet(fetched_pages=(make_page(URL_THREE),))
        with self.assertRaises(ResearchPacketError):
            make_packet(
                fetched_pages=(make_page(URL_ONE), make_page(URL_ONE, "other text"))
            )
        with self.assertRaises(ResearchPacketError):
            make_packet(
                search_results=(make_result(0, URL_ONE), make_result(1, URL_ONE))
            )

    def test_rejects_unranked_or_invalid_results(self):
        with self.assertRaises(ResearchPacketError):
            make_packet(
                search_results=(make_result(1, URL_ONE), make_result(2, URL_TWO))
            )
        # Stored-content problems are ResearchPacketError; only a hash mismatch
        # is the distinct ResearchPacketTamperedError (V57).
        with self.assertRaises(ResearchPacketError):
            SearchResult.from_dict(
                {
                    "position": 0,
                    "site_name": "s",
                    "title": "t",
                    "snippet": "n",
                    "url": "ftp://example.com",
                }
            )


class TestPacketSerialization(unittest.TestCase):
    def test_round_trip_preserves_packet(self):
        packet = make_packet()
        restored = ResearchPacket.from_dict(packet.as_dict())
        self.assertEqual(restored, packet)
        self.assertEqual(restored.packet_sha256, packet.packet_sha256)

    def test_round_trip_preserves_failed_packet(self):
        packet = make_packet(
            status=PacketStatus.FAILED,
            failure_reason=UnresolvedReason.RESEARCH_NO_RESULTS,
            search_results=(),
            fetched_pages=(),
        )
        restored = ResearchPacket.from_dict(packet.as_dict())
        self.assertEqual(restored.failure_reason, UnresolvedReason.RESEARCH_NO_RESULTS)

    def test_edited_page_text_is_detected_as_tampering(self):
        payload = make_packet().as_dict()
        payload["fetched_pages"][0]["text"] = "tampered"
        with self.assertRaises(ResearchPacketTamperedError):
            ResearchPacket.from_dict(payload)

    def test_edited_text_with_recomputed_page_hash_is_detected(self):
        payload = make_packet().as_dict()
        payload["fetched_pages"][0]["text"] = "tampered"
        payload["fetched_pages"][0]["content_sha256"] = hashlib.sha256(
            b"tampered"
        ).hexdigest()
        with self.assertRaises(ResearchPacketTamperedError):
            ResearchPacket.from_dict(payload)

    def test_edited_merchant_cannot_keep_its_packet_id(self):
        payload = make_packet().as_dict()
        payload["normalized_merchant"] = "not acme"
        with self.assertRaises(ResearchPacketMalformedError):
            ResearchPacket.from_dict(payload)

    def test_unknown_or_missing_fields_are_rejected(self):
        payload = make_packet().as_dict()
        payload["extra"] = 1
        with self.assertRaises(ResearchPacketMalformedError):
            ResearchPacket.from_dict(payload)

        payload = make_packet().as_dict()
        del payload["schema_version"]
        with self.assertRaises(ResearchPacketMalformedError):
            ResearchPacket.from_dict(payload)

    def test_unknown_status_and_reason_are_rejected(self):
        payload = make_packet().as_dict()
        payload["status"] = "weird"
        with self.assertRaises(ResearchPacketMalformedError):
            ResearchPacket.from_dict(payload)

        payload = make_packet(
            status=PacketStatus.FAILED,
            failure_reason=UnresolvedReason.RESEARCH_NO_RESULTS,
            search_results=(),
            fetched_pages=(),
        ).as_dict()
        payload["failure_reason"] = "not_a_reason"
        with self.assertRaises(ResearchPacketMalformedError):
            ResearchPacket.from_dict(payload)

    def test_version_drift_round_trips_and_reports_stale(self):
        packet = make_packet(schema_version="transaction-web-research-v0")
        self.assertTrue(packet.is_stale())
        restored = ResearchPacket.from_dict(packet.as_dict())
        self.assertTrue(restored.is_stale())
        self.assertFalse(make_packet().is_stale())

    def test_query_version_drift_is_stale(self):
        self.assertTrue(make_packet(query_version="merchant-research-v1").is_stale())
        self.assertEqual(make_packet().query_version, RESEARCH_QUERY_VERSION)
        self.assertEqual(make_packet().schema_version, RESEARCH_SCHEMA_VERSION)


class TestSuggestionBounds(unittest.TestCase):
    def test_accepts_a_bounded_suggestion(self):
        suggestion = make_suggestion()
        self.assertEqual(suggestion.category_name, "Hobbies")
        self.assertEqual(suggestion.evidence_urls, (URL_ONE,))

    def test_rejects_oversized_names_and_rationale(self):
        with self.assertRaises(ResearchPacketError):
            make_suggestion(category_name="x" * (MAX_SUGGESTION_NAME_CHARS + 1))
        with self.assertRaises(ResearchPacketError):
            make_suggestion(subcategory_name="x" * (MAX_SUGGESTION_NAME_CHARS + 1))
        with self.assertRaises(ResearchPacketError):
            make_suggestion(rationale="x" * (MAX_SUGGESTION_RATIONALE_CHARS + 1))
        self.assertEqual(
            make_suggestion(
                category_name="x" * MAX_SUGGESTION_NAME_CHARS
            ).category_name,
            "x" * MAX_SUGGESTION_NAME_CHARS,
        )

    def test_rejects_blank_and_control_character_values(self):
        for field in ("category_name", "subcategory_name", "rationale"):
            with self.subTest(field=field):
                with self.assertRaises(ResearchPacketError):
                    make_suggestion(**{field: "   "})
                with self.assertRaises(ResearchPacketError):
                    make_suggestion(**{field: "line\nbreak"})

    def test_rejects_evidence_url_count_and_shape(self):
        with self.assertRaises(ResearchPacketError):
            make_suggestion(evidence_urls=())
        with self.assertRaises(ResearchPacketError):
            make_suggestion(evidence_urls=(URL_ONE, URL_TWO, URL_THREE, URL_FOUR))
        with self.assertRaises(ResearchPacketError):
            make_suggestion(evidence_urls=(URL_ONE, URL_ONE))
        with self.assertRaises(ResearchPacketError):
            make_suggestion(evidence_urls=("ftp://example.com",))
        self.assertEqual(
            len(make_suggestion(evidence_urls=(URL_ONE, URL_TWO)).evidence_urls),
            MAX_EVIDENCE_URLS - 1,
        )

    def test_rejects_bad_parent_id_and_packet_hash(self):
        with self.assertRaises(ResearchPacketError):
            make_suggestion(parent_category_id=True)
        with self.assertRaises(ResearchPacketError):
            make_suggestion(research_packet_sha256="short")
        self.assertEqual(make_suggestion(parent_category_id=7).parent_category_id, 7)


class TestSuggestionIdentity(unittest.TestCase):
    def test_suggestion_id_is_deterministic_and_bounded(self):
        suggestion = make_suggestion()
        self.assertEqual(suggestion.suggestion_id, make_suggestion().suggestion_id)
        self.assertEqual(len(suggestion.suggestion_id), SUGGESTION_ID_LENGTH)
        int(suggestion.suggestion_id, 16)

    def test_suggestion_id_changes_with_packet_hash_and_taxonomy(self):
        base = make_suggestion().suggestion_id
        self.assertNotEqual(
            base, make_suggestion(research_packet_sha256="b" * 64).suggestion_id
        )
        self.assertNotEqual(base, make_suggestion(category_name="Sports").suggestion_id)

    def test_round_trip_and_mismatched_id(self):
        suggestion = make_suggestion()
        self.assertEqual(CategorySuggestion.from_dict(suggestion.as_dict()), suggestion)
        payload = suggestion.as_dict()
        payload["suggestion_id"] = "0" * SUGGESTION_ID_LENGTH
        with self.assertRaises(ResearchPacketMalformedError):
            CategorySuggestion.from_dict(payload)


class TestReviewRecord(unittest.TestCase):
    def test_approved_record_has_no_reason(self):
        record = PacketReviewRecord(
            packet_id=packet_id_for("acme"),
            packet_sha256=PACKET_HASH,
            status=PacketReviewStatus.APPROVED,
            reviewed_at=REVIEWED_AT,
        )
        self.assertIsNone(record.reason)
        self.assertEqual(PacketReviewRecord.from_dict(record.as_dict()), record)

    def test_rejected_record_requires_a_bounded_reason(self):
        record = PacketReviewRecord(
            packet_id=packet_id_for("acme"),
            packet_sha256=PACKET_HASH,
            status=PacketReviewStatus.REJECTED,
            reason="  evidence is unrelated  ",
            reviewed_at=REVIEWED_AT,
        )
        self.assertEqual(record.reason, "evidence is unrelated")

        with self.assertRaises(ResearchPacketError):
            PacketReviewRecord(
                packet_id=packet_id_for("acme"),
                packet_sha256=PACKET_HASH,
                status=PacketReviewStatus.REJECTED,
                reviewed_at=REVIEWED_AT,
            )
        with self.assertRaises(ResearchPacketError):
            PacketReviewRecord(
                packet_id=packet_id_for("acme"),
                packet_sha256=PACKET_HASH,
                status=PacketReviewStatus.REJECTED,
                reason="bad\nreason",
                reviewed_at=REVIEWED_AT,
            )

    def test_approved_record_rejects_a_reason_and_pending_is_not_storable(self):
        with self.assertRaises(ResearchPacketError):
            PacketReviewRecord(
                packet_id=packet_id_for("acme"),
                packet_sha256=PACKET_HASH,
                status=PacketReviewStatus.APPROVED,
                reason="why",
                reviewed_at=REVIEWED_AT,
            )
        with self.assertRaises(ResearchPacketError):
            PacketReviewRecord(
                packet_id=packet_id_for("acme"),
                packet_sha256=PACKET_HASH,
                status=PacketReviewStatus.PENDING,
                reviewed_at=REVIEWED_AT,
            )

    def test_record_rejects_bad_hash_and_timestamp(self):
        with self.assertRaises(ResearchPacketError):
            PacketReviewRecord(
                packet_id=packet_id_for("acme"),
                packet_sha256="nope",
                status=PacketReviewStatus.APPROVED,
                reviewed_at=REVIEWED_AT,
            )
        with self.assertRaises(ResearchPacketError):
            PacketReviewRecord(
                packet_id=packet_id_for("acme"),
                packet_sha256=PACKET_HASH,
                status=PacketReviewStatus.APPROVED,
                reviewed_at="2026-09-17T13:00:00",
            )

    def test_unknown_review_status_is_rejected(self):
        with self.assertRaises(ResearchPacketMalformedError):
            PacketReviewRecord.from_dict(
                {
                    "packet_id": packet_id_for("acme"),
                    "packet_sha256": PACKET_HASH,
                    "status": "maybe",
                    "reason": None,
                    "reviewed_at": REVIEWED_AT,
                    "schema_version": RESEARCH_SCHEMA_VERSION,
                    "query_version": RESEARCH_QUERY_VERSION,
                }
            )


class TestTinyFishResearchConfig(unittest.TestCase):
    def test_defaults_match_the_specified_contract(self):
        config = TinyFishResearchConfig(api_key="secret-key")
        self.assertEqual(config.search_timeout_seconds, 30.0)
        self.assertEqual(config.fetch_timeout_seconds, 150.0)
        self.assertEqual(config.location, FIXED_RESEARCH_LOCATION)
        self.assertEqual(config.language, FIXED_RESEARCH_LANGUAGE)

    def test_requires_a_non_blank_api_key(self):
        for value in ("", "   ", None):
            with self.subTest(value=value):
                with self.assertRaises(ResearchConfigError):
                    TinyFishResearchConfig(api_key=value)  # type: ignore[arg-type]

    def test_requires_positive_numeric_timeouts(self):
        for kwargs in (
            {"search_timeout_seconds": 0},
            {"search_timeout_seconds": -1},
            {"fetch_timeout_seconds": 0.0},
            {"search_timeout_seconds": "30"},
            {"fetch_timeout_seconds": True},
        ):
            with self.subTest(kwargs=kwargs):
                with self.assertRaises(ResearchConfigError):
                    TinyFishResearchConfig(api_key="k", **kwargs)  # type: ignore[arg-type]

    def test_rejects_blank_locale_fields(self):
        with self.assertRaises(ResearchConfigError):
            TinyFishResearchConfig(api_key="k", location="  ")
        with self.assertRaises(ResearchConfigError):
            TinyFishResearchConfig(api_key="k", language="")

    def test_repr_never_exposes_the_api_key(self):
        config = TinyFishResearchConfig(api_key="secret-key")
        self.assertNotIn("secret-key", repr(config))
        self.assertIn("***", repr(config))


class TestTypedErrorReasons(unittest.TestCase):
    def test_each_error_maps_to_its_unresolved_reason(self):
        mapping = {
            ResearchAuthError: UnresolvedReason.RESEARCH_AUTH,
            ResearchRateLimitError: UnresolvedReason.RESEARCH_RATE_LIMIT,
            ResearchTimeoutError: UnresolvedReason.RESEARCH_TIMEOUT,
            ResearchProviderError: UnresolvedReason.RESEARCH_PROVIDER_ERROR,
            ResearchNoResultsError: UnresolvedReason.RESEARCH_NO_RESULTS,
            ResearchNoValidUrlsError: UnresolvedReason.RESEARCH_NO_VALID_URLS,
            ResearchFetchFailedError: UnresolvedReason.RESEARCH_FETCH_FAILED,
            ResearchEmptyEvidenceError: UnresolvedReason.RESEARCH_EMPTY_EVIDENCE,
            ResearchIrrelevantError: UnresolvedReason.RESEARCH_IRRELEVANT,
            ResearchMalformedError: UnresolvedReason.RESEARCH_MALFORMED,
        }
        for error_class, expected in mapping.items():
            with self.subTest(error=error_class.__name__):
                self.assertEqual(error_class.reason, expected)
                self.assertIn(expected, RESEARCH_EXECUTION_REASONS)

    def test_config_error_has_no_reason_and_circuit_error_keeps_its_cause(self):
        self.assertIsNone(ResearchConfigError.reason)
        circuit = ResearchCircuitOpenError(
            "circuit open", cause=UnresolvedReason.RESEARCH_RATE_LIMIT
        )
        self.assertEqual(circuit.reason, UnresolvedReason.RESEARCH_RATE_LIMIT)

    def test_research_errors_are_value_errors(self):
        self.assertTrue(issubclass(ResearchAuthError, ValueError))
        self.assertTrue(issubclass(ResearchConfigError, ValueError))


if __name__ == "__main__":
    unittest.main()

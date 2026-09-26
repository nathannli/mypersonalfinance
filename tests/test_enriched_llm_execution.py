"""Tests for the enriched execution path (T8).

Pins that the enriched request carries the T7 prompt/schema and bounded
evidence, that HTTP and circuit semantics match the unenriched client, that
results are cached only under packet-bound identity, and that parser failures
keep their typed reasons.
"""

from __future__ import annotations

import json
import unittest

from services.llm_categorizer import (
    ENRICHED_SCHEMA_VERSION,
    ENRICHED_SYSTEM_PROMPT,
    EnrichedAbstain,
    EnrichedExecution,
    EnrichedSelect,
    EnrichedSuggestion,
    OpenCodexConfig,
    OpenCodexCategorizer,
)
from services.research_packets import (
    FetchedPage,
    PacketStatus,
    ResearchPacket,
    SearchResult,
)
from services.transaction_categorization import (
    CanonicalContext,
    UnresolvedReason,
    canonicalize_choices,
)

EVIDENCE_URL = "https://acme.example/about"
CHOICES = [
    {
        "subcategory_id": 11,
        "category_id": 1,
        "subcategory_name": "Eating Out",
        "category_name": "Food",
    },
]


def make_context(packet: ResearchPacket | None = None) -> CanonicalContext:
    """Build the canonical context, optionally bound to one packet's identity.

    An enriched request only ever binds a fully identified packet (V30); the
    unbound form is kept so tests can prove the mismatch fails closed.
    """
    return CanonicalContext(
        database="finance",
        merchant="acme widgets",
        amount_minor_units=1234,
        statement_category=None,
        allowed_choices=canonicalize_choices("finance", CHOICES),
        research_packet_sha256=None if packet is None else packet.packet_sha256,
        research_packet_schema_version=(
            None if packet is None else packet.schema_version
        ),
        research_packet_query_version=(
            None if packet is None else packet.query_version
        ),
    )


def make_packet(text: str = "Acme makes widgets for industry.") -> ResearchPacket:
    return ResearchPacket(
        normalized_merchant="acme widgets",
        derived_query="acme widgets",
        status=PacketStatus.COMPLETE,
        searched_at="2026-09-17T12:00:00+00:00",
        search_results=(
            SearchResult(
                position=0,
                site_name="Acme",
                title="Acme Widgets",
                snippet="We make widgets",
                url=EVIDENCE_URL,
            ),
        ),
        fetched_pages=(
            FetchedPage(
                url=EVIDENCE_URL,
                final_url=EVIDENCE_URL,
                title="Acme Widgets",
                description="A widget maker",
                text=text,
                relevance_matched_tokens=("acme", "widgets"),
            ),
        ),
    )


def envelope(payload) -> dict:
    if isinstance(payload, str):
        content = payload
    else:
        content = json.dumps(payload)
    return {"choices": [{"message": {"content": content}}]}


class FakeResponse:
    def __init__(self, payload, status_code: int = 200):
        self._payload = payload
        self.status_code = status_code

    def json(self):
        if isinstance(self._payload, Exception):
            raise self._payload
        return self._payload


class RecordingPost:
    def __init__(self, response):
        self.response = response
        self.calls: list[dict] = []

    def __call__(self, url, data=None, headers=None, timeout=None):
        self.calls.append(
            {"url": url, "data": data, "headers": headers, "timeout": timeout}
        )
        return self.response


class RaisingPost:
    """Raises from the transport itself, as requests does on a real timeout."""

    def __init__(self, exception):
        self.exception = exception
        self.calls: list[str] = []

    def __call__(self, url, data=None, headers=None, timeout=None):
        self.calls.append(url)
        raise self.exception


def as_response(payload):
    """Wrap a raw payload so status_code and json() behave like a response."""
    if hasattr(payload, "status_code"):
        return payload
    return FakeResponse(payload)


def build(response, *, api_key: str = "test-key", mode: str = "shadow"):
    post = RecordingPost(as_response(response))
    return build_with(post, api_key=api_key, mode=mode), post


def build_with(post, *, api_key: str = "test-key", mode: str = "shadow"):
    config = OpenCodexConfig(
        base_url="http://localhost:10100",
        api_key=api_key,
        model="test-model",
        mode=mode,
    )
    return OpenCodexCategorizer(config, post=post)


class TestExecutionInvariants(unittest.TestCase):
    def test_exactly_one_of_decision_or_reason(self):
        with self.assertRaises(ValueError):
            EnrichedExecution()
        with self.assertRaises(ValueError):
            EnrichedExecution(
                decision=EnrichedAbstain(reason="x", context_fingerprint="f"),
                reason=UnresolvedReason.TIMEOUT,
            )


class TestEnrichedRequest(unittest.TestCase):
    def setUp(self) -> None:
        self.packet = make_packet()
        self.context = make_context(self.packet)

    def test_request_uses_the_enriched_protocol_and_bounded_evidence(self):
        categorizer, post = build(envelope({"action": "abstain", "reason": "unclear"}))
        categorizer.categorize_enriched(self.context, self.packet)

        call = post.calls[0]
        payload = json.loads(call["data"])
        self.assertEqual(call["url"], "http://localhost:10100/v1/chat/completions")
        self.assertEqual(payload["model"], "test-model")
        self.assertEqual(payload["temperature"], 0)
        self.assertIs(payload["stream"], False)

        descriptor = payload["response_format"]["json_schema"]
        self.assertEqual(descriptor["name"], ENRICHED_SCHEMA_VERSION)
        self.assertIs(descriptor["strict"], True)

        messages = payload["messages"]
        self.assertEqual(messages[0]["content"], ENRICHED_SYSTEM_PROMPT)
        user_content = json.loads(messages[1]["content"])
        self.assertEqual(
            set(user_content), {"allowed_choices", "research_packet", "transaction"}
        )
        evidence = json.dumps(user_content["research_packet"])
        self.assertIn(EVIDENCE_URL, evidence)
        self.assertIn("Acme makes widgets for industry.", evidence)

    def test_authorization_header_is_sent(self):
        categorizer, post = build(envelope({"action": "abstain", "reason": "unclear"}))
        categorizer.categorize_enriched(self.context, self.packet)

        self.assertEqual(post.calls[0]["headers"]["Authorization"], "Bearer test-key")

    def test_provider_call_count_increments(self):
        categorizer, _ = build(envelope({"action": "abstain", "reason": "unclear"}))
        categorizer.categorize_enriched(self.context, self.packet)

        self.assertEqual(categorizer.provider_call_count, 1)


class TestEnrichedDecisions(unittest.TestCase):
    def setUp(self) -> None:
        self.packet = make_packet()
        self.context = make_context(self.packet)

    def run_with(self, payload) -> EnrichedExecution:
        categorizer, _ = build(envelope(payload))
        return categorizer.categorize_enriched(self.context, self.packet)

    def test_select_decision(self):
        execution = self.run_with(
            {"action": "select", "choice_id": 11, "evidence_urls": [EVIDENCE_URL]}
        )
        self.assertIsInstance(execution.decision, EnrichedSelect)
        self.assertEqual(execution.decision.choice_id, 11)

    def test_suggest_new_decision(self):
        execution = self.run_with(
            {
                "action": "suggest_new",
                "category_name": "Gadgets",
                "subcategory_name": "Widgets",
                "parent_category_id": None,
                "rationale": "No offered choice covers widget vendors.",
                "evidence_urls": [EVIDENCE_URL],
            }
        )
        self.assertIsInstance(execution.decision, EnrichedSuggestion)
        self.assertEqual(execution.decision.category_name, "Gadgets")

    def test_abstain_decision(self):
        execution = self.run_with({"action": "abstain", "reason": "unclear"})
        self.assertIsInstance(execution.decision, EnrichedAbstain)

    def test_validation_failure_keeps_its_typed_reason(self):
        # An unoffered choice is a semantic failure, not a protocol one.
        execution = self.run_with(
            {"action": "select", "choice_id": 999, "evidence_urls": [EVIDENCE_URL]}
        )
        self.assertIsNone(execution.decision)
        self.assertIs(execution.reason, UnresolvedReason.INVALID_CHOICE)

    def test_malformed_content_keeps_the_malformed_reason(self):
        categorizer, _ = build(envelope("not json at all"))
        execution = categorizer.categorize_enriched(self.context, self.packet)

        self.assertIsNone(execution.decision)
        self.assertIs(execution.reason, UnresolvedReason.MALFORMED)


class TestEnrichedCache(unittest.TestCase):
    def select_payload(self) -> dict:
        return {"action": "select", "choice_id": 11, "evidence_urls": [EVIDENCE_URL]}

    def test_repeat_select_is_served_from_the_packet_bound_cache(self):
        categorizer, post = build(envelope(self.select_payload()))
        packet = make_packet()
        context = make_context(packet)

        first = categorizer.categorize_enriched(context, packet)
        second = categorizer.categorize_enriched(context, packet)

        self.assertEqual(len(post.calls), 1)
        self.assertEqual(categorizer.cache_hit_count, 1)
        self.assertEqual(first.decision, second.decision)

    def test_suggestion_and_abstain_are_never_cached(self):
        payloads = (
            {
                "action": "suggest_new",
                "category_name": "Hobbies",
                "subcategory_name": "Sewing",
                "parent_category_id": None,
                "rationale": "No live choice covers sewing supplies.",
                "evidence_urls": [EVIDENCE_URL],
            },
            {"action": "abstain", "reason": "Opaque descriptor."},
        )
        for payload in payloads:
            with self.subTest(action=payload["action"]):
                categorizer, post = build(envelope(payload))
                packet = make_packet()
                context = make_context(packet)

                categorizer.categorize_enriched(context, packet)
                categorizer.categorize_enriched(context, packet)

                self.assertEqual(len(post.calls), 2)
                self.assertEqual(categorizer.cache_hit_count, 0)

    def test_failures_are_never_cached(self):
        categorizer, post = build(FakeResponse({}, status_code=503))
        packet = make_packet()
        context = make_context(packet)

        categorizer.categorize_enriched(context, packet)

        self.assertEqual(categorizer.cache_hit_count, 0)
        self.assertEqual(len(post.calls), 1)

    def test_a_refreshed_packet_is_a_different_cache_entry(self):
        categorizer, post = build(envelope(self.select_payload()))
        original = make_packet()
        refreshed = make_packet(text="Refreshed evidence text.")

        categorizer.categorize_enriched(make_context(original), original)
        categorizer.categorize_enriched(make_context(refreshed), refreshed)

        self.assertNotEqual(original.packet_sha256, refreshed.packet_sha256)
        self.assertEqual(len(post.calls), 2)
        self.assertEqual(categorizer.cache_hit_count, 0)

    def test_context_and_packet_identity_mismatch_fails_closed(self):
        categorizer, post = build(envelope(self.select_payload()))
        packet = make_packet()

        execution = categorizer.categorize_enriched(make_context(), packet)

        self.assertIs(execution.reason, UnresolvedReason.INVALID_CONTEXT)
        self.assertEqual(post.calls, [])
        self.assertEqual(categorizer.cache_hit_count, 0)

    def test_a_stale_schema_version_on_the_context_fails_closed(self):
        from dataclasses import replace

        categorizer, post = build(envelope(self.select_payload()))
        packet = make_packet()
        stale = replace(
            make_context(packet),
            research_packet_schema_version="transaction-web-research-v0",
        )

        execution = categorizer.categorize_enriched(stale, packet)

        self.assertIs(execution.reason, UnresolvedReason.INVALID_CONTEXT)
        self.assertEqual(post.calls, [])


class TestEnrichedFailureHandling(unittest.TestCase):
    def setUp(self) -> None:
        self.packet = make_packet()
        self.context = make_context(self.packet)

    def test_blank_api_key_opens_the_circuit_without_a_request(self):
        categorizer, post = build(
            envelope({"action": "abstain", "reason": "x"}), api_key=""
        )
        execution = categorizer.categorize_enriched(self.context, self.packet)

        self.assertIs(execution.reason, UnresolvedReason.PROVIDER_ERROR)
        self.assertTrue(categorizer.circuit_open)
        self.assertEqual(post.calls, [])

    def test_non_2xx_opens_the_circuit_immediately(self):
        categorizer, _ = build(FakeResponse({}, status_code=500))
        execution = categorizer.categorize_enriched(self.context, self.packet)

        self.assertIs(execution.reason, UnresolvedReason.PROVIDER_ERROR)
        self.assertTrue(categorizer.circuit_open)

    def test_a_single_transport_error_is_retryable_before_the_circuit_opens(self):
        categorizer, _ = build(FakeResponse({}, status_code=503))
        categorizer.categorize_enriched(self.context, self.packet)
        self.assertTrue(categorizer.circuit_open)

    def test_timeout_is_typed_and_only_opens_after_two_failures(self):
        import requests

        # requests.Timeout is raised by the transport, not by response.json().
        categorizer = build_with(RaisingPost(requests.Timeout()))
        first = categorizer.categorize_enriched(self.context, self.packet)
        self.assertIs(first.reason, UnresolvedReason.TIMEOUT)
        self.assertFalse(categorizer.circuit_open)

        second = categorizer.categorize_enriched(self.context, self.packet)
        self.assertIs(second.reason, UnresolvedReason.TIMEOUT)
        self.assertTrue(categorizer.circuit_open)

    def test_unparseable_body_is_malformed(self):
        categorizer, _ = build(FakeResponse(ValueError("not json")))
        execution = categorizer.categorize_enriched(self.context, self.packet)

        self.assertIs(execution.reason, UnresolvedReason.MALFORMED)

    def test_an_open_circuit_issues_no_further_request(self):
        categorizer, post = build(FakeResponse({}, status_code=401))
        categorizer.categorize_enriched(self.context, self.packet)
        calls_after_open = len(post.calls)

        for _ in range(3):
            execution = categorizer.categorize_enriched(self.context, self.packet)
            self.assertIs(execution.reason, UnresolvedReason.CIRCUIT_OPEN)

        self.assertEqual(len(post.calls), calls_after_open)

    def test_success_resets_the_failure_streak(self):
        categorizer, _ = build(FakeResponse(ValueError("not json")))
        categorizer.categorize_enriched(self.context, self.packet)
        self.assertEqual(categorizer.consecutive_failures, 1)

        categorizer._post = RecordingPost(
            FakeResponse(envelope({"action": "abstain", "reason": "unclear"}))
        )
        categorizer.categorize_enriched(self.context, self.packet)

        self.assertEqual(categorizer.consecutive_failures, 0)


if __name__ == "__main__":
    unittest.main()

"""Tests for the research runner: discovery, operations, and run status (T6)."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from services import research_runner as rr
from services.research_packets import (
    RESEARCH_EXECUTION_REASONS,
    FetchedPage,
    PacketReviewRecord,
    PacketReviewStatus,
    PacketStatus,
    ResearchPacket,
    ResearchRunStatus,
    SearchResult,
    load_packet,
    record_review,
    review_record_for,
    store_failure_packet,
    store_packet,
)
from services.research_runner import (
    TargetOperation,
    TargetOutcome,
    ResearchTarget,
    discover_targets,
    run_target,
    run_targets,
    summarize,
)
from services.tinyfish_research import (
    ResearchAuthError,
    ResearchCircuitOpenError,
    ResearchEmptyEvidenceError,
    ResearchNoResultsError,
    ResearchProviderError,
    ResearchTimeoutError,
)

SEARCH_URL = "https://acme.example/about"
OTHER_URL = "https://acme.example/pricing"

CHOICES = [
    {
        "subcategory_id": 13,
        "category_id": 1,
        "subcategory_name": "Grocery",
        "category_name": "Food",
    },
]

RELEVANT_TEXT = "Acme makes widgets for industrial customers."


def target(merchant: str = "acme widgets") -> ResearchTarget:
    from services.transaction_categorization import normalize_context_text

    normalized = normalize_context_text(merchant)
    return ResearchTarget(normalized, merchant)


def result(position: int, url: str) -> SearchResult:
    return SearchResult(
        position=position,
        site_name="Acme",
        title="Acme Widgets",
        snippet="We make widgets",
        url=url,
    )


def page(
    url: str,
    text: str = RELEVANT_TEXT,
    *,
    title: str = "Acme Widgets",
    description: str = "A widget maker",
) -> FetchedPage:
    return FetchedPage(
        url=url,
        final_url=url,
        title=title,
        description=description,
        text=text,
    )


def complete_packet(merchant: str = "acme widgets", **overrides) -> ResearchPacket:
    values = {
        "normalized_merchant": merchant,
        "derived_query": merchant,
        "status": PacketStatus.COMPLETE,
        "searched_at": "2026-09-17T12:00:00+00:00",
        "search_results": (result(0, SEARCH_URL),),
        "fetched_pages": (page(SEARCH_URL),),
    }
    values.update(overrides)
    return ResearchPacket(**values)


def failure_packet(
    merchant: str = "acme widgets", reason=rr.UnresolvedReason.RESEARCH_TIMEOUT
) -> ResearchPacket:
    return ResearchPacket(
        normalized_merchant=merchant,
        derived_query=merchant,
        status=PacketStatus.FAILED,
        searched_at="2026-09-17T12:00:00+00:00",
        failure_reason=reason,
    )


class FakeClient:
    """Stands in for TinyFishClient; records every request (V3)."""

    def __init__(
        self,
        *,
        results=(),
        pages=None,
        search_error: Exception | None = None,
        fetch_errors=None,
    ):
        self.results = tuple(results)
        self.pages = pages or {}
        self.search_error = search_error
        self.fetch_errors = fetch_errors or {}
        self.search_calls: list[str] = []
        self.fetch_calls: list[str] = []
        self.circuit_open = False
        self.circuit_reason = None

    def search(self, derived_query: str):
        self.search_calls.append(derived_query)
        if self.search_error is not None:
            raise self.search_error
        return self.results

    def fetch(self, url: str):
        self.fetch_calls.append(url)
        error = self.fetch_errors.get(url)
        if error is not None:
            raise error
        return self.pages[url]


def ok_client(**kwargs) -> FakeClient:
    kwargs.setdefault("results", (result(0, SEARCH_URL),))
    kwargs.setdefault("pages", {SEARCH_URL: page(SEARCH_URL)})
    return FakeClient(**kwargs)


class RunnerTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self._temporary = tempfile.TemporaryDirectory()
        self.root = Path(self._temporary.name)

    def tearDown(self) -> None:
        self._temporary.cleanup()


class TestDiscovery(RunnerTestCase):
    def discover(self, rows, *, card_type="amex", auto_match=None):
        return discover_targets(
            rows,
            card_type=card_type,
            choices=CHOICES,
            auto_match=auto_match or (lambda merchant: None),
        )

    def test_unknown_rows_become_targets(self):
        found = self.discover([{"merchant": "Acme Widgets", "cc_category": None}])
        self.assertEqual(len(found), 1)
        self.assertEqual(found[0].normalized_merchant, "acme widgets")
        self.assertEqual(found[0].raw_merchant, "Acme Widgets")

    def test_deduplicates_by_normalized_merchant(self):
        found = self.discover(
            [
                {"merchant": "Acme Widgets", "cc_category": None},
                {"merchant": "ACME  WIDGETS", "cc_category": None},
                {"merchant": "  acme widgets  ", "cc_category": None},
            ]
        )
        self.assertEqual(len(found), 1)
        # The first occurrence supplies the display form.
        self.assertEqual(found[0].raw_merchant, "Acme Widgets")

    def test_deterministically_matched_rows_are_skipped(self):
        found = self.discover(
            [{"merchant": "Known Store", "cc_category": None}],
            auto_match=lambda merchant: ("Food", "Grocery"),
        )
        self.assertEqual(found, ())

    def test_invalid_mapping_rows_are_skipped(self):
        # A mapping exists that the taxonomy cannot honour, so researching the
        # merchant would hide a data bug needing a human.
        found = self.discover(
            [{"merchant": "Stale Store", "cc_category": None}],
            auto_match=lambda merchant: ("Nonexistent", "Nowhere"),
        )
        self.assertEqual(found, ())

    def test_blank_merchants_are_skipped(self):
        found = self.discover(
            [
                {"merchant": "", "cc_category": None},
                {"merchant": "   ", "cc_category": None},
                {"merchant": None, "cc_category": None},
                {"merchant": "Acme Widgets", "cc_category": None},
            ]
        )
        self.assertEqual(len(found), 1)

    def test_first_seen_order_is_preserved(self):
        found = self.discover(
            [
                {"merchant": "Zeta Corp", "cc_category": None},
                {"merchant": "Alpha Inc", "cc_category": None},
                {"merchant": "Zeta Corp", "cc_category": None},
            ]
        )
        self.assertEqual(
            [t.normalized_merchant for t in found], ["zeta corp", "alpha inc"]
        )

    def test_statement_category_reaches_the_resolver(self):
        # Rogers rows resolve from their statement category, so a matching one
        # must be treated as known rather than researched.
        found = self.discover(
            [{"merchant": "Some Cafe", "cc_category": "Eating Places and Restaurants"}],
            card_type="rogers",
        )
        self.assertEqual(found, ())


class TestRunStatus(RunnerTestCase):
    def result(self, outcome: TargetOutcome, operation=TargetOperation.RESEARCH):
        return rr.ResearchTargetResult(
            normalized_merchant="m",
            operation=operation,
            outcome=outcome,
            packet_id="a" * 64,
        )

    def test_zero_targets_is_complete(self):
        summary = summarize(())
        self.assertEqual(summary.status, ResearchRunStatus.COMPLETE)
        self.assertEqual(summary.exit_code, 0)
        self.assertEqual(summary.total, 0)

    def test_every_operation_succeeding_is_complete(self):
        summary = summarize([self.result(TargetOutcome.SUCCEEDED)] * 3)
        self.assertEqual(summary.status, ResearchRunStatus.COMPLETE)
        self.assertEqual(summary.exit_code, 0)

    def test_one_success_and_one_failure_is_partial(self):
        summary = summarize(
            [self.result(TargetOutcome.SUCCEEDED), self.result(TargetOutcome.FAILED)]
        )
        self.assertEqual(summary.status, ResearchRunStatus.PARTIAL)
        self.assertEqual(summary.exit_code, 0)

    def test_no_successes_over_a_nonempty_target_set_is_failed(self):
        summary = summarize([self.result(TargetOutcome.FAILED)] * 2)
        self.assertEqual(summary.status, ResearchRunStatus.FAILED)
        self.assertEqual(summary.exit_code, 1)

    def test_counts_and_separation_of_refresh_failures(self):
        results = [
            self.result(TargetOutcome.SUCCEEDED, TargetOperation.REUSE),
            self.result(TargetOutcome.SUCCEEDED, TargetOperation.RESEARCH),
            self.result(TargetOutcome.SUCCEEDED, TargetOperation.REFRESH),
            self.result(TargetOutcome.FAILED, TargetOperation.REFRESH),
            self.result(TargetOutcome.FAILED, TargetOperation.RESEARCH),
        ]
        summary = summarize(results)
        self.assertEqual(summary.total, 5)
        self.assertEqual(summary.succeeded, 3)
        self.assertEqual(summary.failed, 2)
        self.assertEqual(summary.reused, 1)
        self.assertEqual(summary.researched, 1)
        self.assertEqual(summary.refreshed, 1)
        self.assertEqual(len(summary.refresh_failures), 1)
        self.assertEqual(len(summary.failures), 2)
        # A failed refresh is reported separately from live evidence (V60).
        self.assertIs(summary.refresh_failures[0].operation, TargetOperation.REFRESH)


class TestReuseAndStaleness(RunnerTestCase):
    def test_current_packet_is_reused_with_zero_requests(self):
        stored = complete_packet()
        store_packet(stored, root=self.root)
        client = ok_client()

        outcome = run_target(target(), client=client, root=self.root)

        self.assertIs(outcome.operation, TargetOperation.REUSE)
        self.assertIs(outcome.outcome, TargetOutcome.SUCCEEDED)
        self.assertEqual(client.search_calls, [])
        self.assertEqual(client.fetch_calls, [])
        self.assertEqual(
            load_packet(outcome.packet_id, root=self.root).packet_sha256,
            stored.packet_sha256,
        )

    def test_reuse_reports_existing_review_state(self):
        stored = complete_packet()
        store_packet(stored, root=self.root)
        record_review(
            PacketReviewRecord(
                packet_id=stored.packet_id,
                packet_sha256=stored.packet_sha256,
                status=PacketReviewStatus.APPROVED,
                reviewed_at="2026-09-17T13:00:00+00:00",
            ),
            root=self.root,
        )
        outcome = run_target(target(), client=ok_client(), root=self.root)
        self.assertIs(outcome.review_status, PacketReviewStatus.APPROVED)

    def test_stale_packet_requires_refresh_and_is_not_replaced(self):
        stale = complete_packet(schema_version="transaction-web-research-v0")
        path = store_packet(stale, root=self.root)
        before = path.read_bytes()
        client = ok_client()

        outcome = run_target(target(), client=client, root=self.root)

        self.assertIs(outcome.outcome, TargetOutcome.FAILED)
        self.assertIs(outcome.failure_reason, rr.UnresolvedReason.RESEARCH_STALE)
        # V13/V57: no silent refresh of an existing packet.
        self.assertEqual(client.search_calls, [])
        self.assertEqual(path.read_bytes(), before)

    def test_failure_packet_is_researched_again(self):
        failure = failure_packet()
        store_failure_packet(failure, root=self.root)
        client = ok_client()

        outcome = run_target(target(), client=client, root=self.root)

        self.assertIs(outcome.operation, TargetOperation.RESEARCH)
        self.assertIs(outcome.outcome, TargetOutcome.SUCCEEDED)
        self.assertEqual(len(client.search_calls), 1)
        self.assertIs(
            load_packet(outcome.packet_id, root=self.root).status, PacketStatus.COMPLETE
        )


class TestInitialResearch(RunnerTestCase):
    def test_successful_research_stores_a_complete_pending_packet(self):
        outcome = run_target(target(), client=ok_client(), root=self.root)

        self.assertIs(outcome.outcome, TargetOutcome.SUCCEEDED)
        self.assertIs(outcome.operation, TargetOperation.RESEARCH)
        self.assertIs(outcome.review_status, PacketReviewStatus.PENDING)
        packet = load_packet(outcome.packet_id, root=self.root)
        self.assertIs(packet.status, PacketStatus.COMPLETE)
        self.assertEqual(packet.packet_sha256, outcome.packet_sha256)
        self.assertEqual(packet.derived_query, "acme widgets")

    def test_packet_pages_come_only_from_retained_search_results(self):
        outcome = run_target(target(), client=ok_client(), root=self.root)
        packet = load_packet(outcome.packet_id, root=self.root)
        urls = {r.url for r in packet.search_results}
        for page in packet.fetched_pages:
            self.assertIn(page.url, urls)

    def test_relevance_tokens_are_recorded_on_pages(self):
        outcome = run_target(target(), client=ok_client(), root=self.root)
        packet = load_packet(outcome.packet_id, root=self.root)
        self.assertTrue(packet.fetched_pages[0].relevance_matched_tokens)

    def test_fetch_is_one_request_per_url_and_capped(self):
        client = ok_client(
            results=(
                result(0, SEARCH_URL),
                result(1, OTHER_URL),
                result(2, "https://acme.example/third"),
                result(3, "https://acme.example/fourth"),
            ),
            pages={
                SEARCH_URL: page(SEARCH_URL),
                OTHER_URL: page(OTHER_URL),
                "https://acme.example/third": page("https://acme.example/third"),
                "https://acme.example/fourth": page("https://acme.example/fourth"),
            },
        )
        run_target(target(), client=client, root=self.root)
        # V26/V42: at most three fetches, each its own request.
        self.assertEqual(len(client.fetch_calls), 3)
        self.assertEqual(client.fetch_calls[0], SEARCH_URL)


class TestFailurePrecedence(RunnerTestCase):
    """V29 ordering, exercised end to end through run_target."""

    def reason_for(self, client, *, merchant="acme widgets"):
        outcome = run_target(target(merchant), client=client, root=self.root)
        self.assertIs(outcome.outcome, TargetOutcome.FAILED)
        return outcome.failure_reason, outcome

    def test_no_significant_tokens_makes_no_request(self):
        client = ok_client()
        reason, _ = self.reason_for(client, merchant="ab cd")
        self.assertIs(reason, rr.UnresolvedReason.RESEARCH_IRRELEVANT)
        self.assertEqual(client.search_calls, [])
        self.assertEqual(client.fetch_calls, [])

    def test_provider_no_results(self):
        client = FakeClient(search_error=ResearchNoResultsError("none"))
        reason, _ = self.reason_for(client)
        self.assertIs(reason, rr.UnresolvedReason.RESEARCH_NO_RESULTS)

    def test_all_fetches_failing_with_one_reason_preserves_it(self):
        client = ok_client(
            results=(result(0, SEARCH_URL), result(1, OTHER_URL)),
            pages={},
            fetch_errors={
                SEARCH_URL: ResearchTimeoutError("slow"),
                OTHER_URL: ResearchTimeoutError("slow"),
            },
        )
        reason, _ = self.reason_for(client)
        self.assertIs(reason, rr.UnresolvedReason.RESEARCH_TIMEOUT)

    def test_mixed_fetch_failures_become_fetch_failed(self):
        client = ok_client(
            results=(result(0, SEARCH_URL), result(1, OTHER_URL)),
            pages={},
            fetch_errors={
                SEARCH_URL: ResearchTimeoutError("slow"),
                OTHER_URL: ResearchProviderError("boom"),
            },
        )
        reason, _ = self.reason_for(client)
        self.assertIs(reason, rr.UnresolvedReason.RESEARCH_FETCH_FAILED)

    def test_all_fetches_yielding_no_text_is_empty_evidence(self):
        # V61: every fetch succeeded but yielded nothing, so the outcome is
        # empty evidence rather than the shared-terminal-reason path.
        client = ok_client(
            results=(result(0, SEARCH_URL), result(1, OTHER_URL)),
            pages={},
            fetch_errors={
                SEARCH_URL: ResearchEmptyEvidenceError("blank"),
                OTHER_URL: ResearchEmptyEvidenceError("blank"),
            },
        )
        reason, _ = self.reason_for(client)
        self.assertIs(reason, rr.UnresolvedReason.RESEARCH_EMPTY_EVIDENCE)

    def test_a_failed_fetch_mixed_with_a_blank_page_is_empty_evidence(self):
        # V61 (V29 clause 6): a zero-character fetch is a *successful* fetch, so
        # the fetches did not all fail: the outcome is empty evidence, not the
        # mixed-failure reason that clause 5 reserves for genuine failures.
        client = ok_client(
            results=(result(0, SEARCH_URL), result(1, OTHER_URL)),
            pages={},
            fetch_errors={
                SEARCH_URL: ResearchTimeoutError("slow"),
                OTHER_URL: ResearchEmptyEvidenceError("blank"),
            },
        )
        reason, _ = self.reason_for(client)
        self.assertIs(reason, rr.UnresolvedReason.RESEARCH_EMPTY_EVIDENCE)

    def test_blank_page_mixed_with_a_shared_failure_reason_keeps_nothing(self):
        # V61: two genuine failures share one reason, but the third fetch
        # succeeded with no characters, so V29 clause 4's shared-reason
        # preservation does not apply and the outcome is empty evidence.
        client = ok_client(
            results=(
                result(0, SEARCH_URL),
                result(1, OTHER_URL),
                result(2, "https://three.example/page"),
            ),
            pages={},
            fetch_errors={
                SEARCH_URL: ResearchTimeoutError("slow"),
                OTHER_URL: ResearchTimeoutError("slow"),
                "https://three.example/page": ResearchEmptyEvidenceError("blank"),
            },
        )
        reason, _ = self.reason_for(client)
        self.assertIs(reason, rr.UnresolvedReason.RESEARCH_EMPTY_EVIDENCE)

    def test_retrieved_but_irrelevant_text_outranks_a_fetch_failure(self):
        # V29 rule 7 outranks rule 5: fetch_failed requires EVERY fetch to have
        # failed, and this run retrieved bounded text that simply said nothing
        # about the merchant.
        client = ok_client(
            results=(result(0, SEARCH_URL), result(1, OTHER_URL)),
            pages={
                SEARCH_URL: page(
                    SEARCH_URL,
                    text="A bicycle repair shop.",
                    title="City Cycles",
                    description="Repairs and sales",
                )
            },
            fetch_errors={OTHER_URL: ResearchTimeoutError("slow")},
        )
        reason, _ = self.reason_for(client)
        self.assertIs(reason, rr.UnresolvedReason.RESEARCH_IRRELEVANT)

    def test_blank_text_everywhere_is_empty_evidence(self):
        client = ok_client(
            results=(result(0, SEARCH_URL), result(1, OTHER_URL)),
            pages={},
            fetch_errors={
                SEARCH_URL: ResearchEmptyEvidenceError("blank"),
                OTHER_URL: ResearchEmptyEvidenceError("blank"),
            },
        )
        reason, _ = self.reason_for(client)
        self.assertIs(reason, rr.UnresolvedReason.RESEARCH_EMPTY_EVIDENCE)

    def test_text_with_no_relevant_page_is_irrelevant(self):
        # Relevance is checked against title, description and text, so a page
        # that matches on any of them counts (V45).
        client = ok_client(
            pages={
                SEARCH_URL: page(
                    SEARCH_URL,
                    "DNG files are a raw image format.",
                    title="DNG file format",
                    description="How to open DNG files",
                )
            }
        )
        reason, _ = self.reason_for(client)
        self.assertIs(reason, rr.UnresolvedReason.RESEARCH_IRRELEVANT)

    def test_a_matching_title_alone_retains_the_page(self):
        client = ok_client(
            pages={
                SEARCH_URL: page(
                    SEARCH_URL,
                    "Unrelated body copy.",
                    title="Acme Widgets",
                    description="",
                )
            }
        )
        outcome = run_target(target(), client=client, root=self.root)
        self.assertIs(outcome.outcome, TargetOutcome.SUCCEEDED)

    def test_circuit_open_failure_uses_its_cause_reason(self):
        from services.tinyfish_research import ResearchCircuitOpenError

        client = FakeClient(
            search_error=ResearchCircuitOpenError(
                "open", cause=rr.UnresolvedReason.RESEARCH_AUTH
            )
        )
        reason, _ = self.reason_for(client)
        self.assertIs(reason, rr.UnresolvedReason.RESEARCH_AUTH)

    def test_unexpected_failure_does_not_become_a_state_reason(self):
        client = FakeClient(search_error=RuntimeError("kaboom"))
        outcome = run_target(target(), client=client, root=self.root)
        self.assertIs(outcome.outcome, TargetOutcome.FAILED)
        self.assertIs(
            outcome.failure_reason, rr.UnresolvedReason.RESEARCH_PROVIDER_ERROR
        )
        # The stored packet must still carry a valid execution reason (V55).
        packet = load_packet(outcome.packet_id, root=self.root)
        self.assertIs(packet.status, PacketStatus.FAILED)
        self.assertIn(packet.failure_reason, RESEARCH_EXECUTION_REASONS)

    def test_initial_failure_writes_a_failure_packet(self):
        client = FakeClient(search_error=ResearchNoResultsError("none"))
        outcome = run_target(target(), client=client, root=self.root)
        packet = load_packet(outcome.packet_id, root=self.root)
        self.assertIs(packet.status, PacketStatus.FAILED)
        self.assertEqual(packet.fetched_pages, ())
        self.assertIs(packet.failure_reason, rr.UnresolvedReason.RESEARCH_NO_RESULTS)

    def test_failure_packet_records_the_derived_query_not_the_descriptor(self):
        # V44: a failed packet still records the deterministic term that was
        # used, so a failure can be diagnosed without re-deriving by hand.
        merchant = "paypal *acme widgets 4471 toronto"
        client = FakeClient(search_error=ResearchNoResultsError("none"))
        outcome = run_target(target(merchant), client=client, root=self.root)

        packet = load_packet(outcome.packet_id, root=self.root)
        self.assertEqual(packet.derived_query, "acme widgets")
        self.assertNotEqual(packet.derived_query, packet.normalized_merchant)

    def test_unsearchable_descriptor_leaves_no_derived_query(self):
        # A descriptor that derives nothing usable has no term to record, so
        # the field stays empty rather than echoing the raw descriptor (V44).
        client = ok_client()
        outcome = run_target(target("4471"), client=client, root=self.root)

        packet = load_packet(outcome.packet_id, root=self.root)
        self.assertIs(packet.status, PacketStatus.FAILED)
        self.assertIs(packet.failure_reason, rr.UnresolvedReason.RESEARCH_IRRELEVANT)
        self.assertEqual(packet.derived_query, "")


class TestRefresh(RunnerTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.stored = complete_packet()
        store_packet(self.stored, root=self.root)
        record_review(
            PacketReviewRecord(
                packet_id=self.stored.packet_id,
                packet_sha256=self.stored.packet_sha256,
                status=PacketReviewStatus.APPROVED,
                reviewed_at="2026-09-17T13:00:00+00:00",
            ),
            root=self.root,
        )

    def test_successful_refresh_replaces_and_returns_to_pending(self):
        client = ok_client(
            pages={SEARCH_URL: page(SEARCH_URL, "Acme widgets, now with widgets.")}
        )
        outcome = run_target(target(), client=client, refresh=True, root=self.root)

        self.assertIs(outcome.operation, TargetOperation.REFRESH)
        self.assertIs(outcome.outcome, TargetOutcome.SUCCEEDED)
        self.assertIs(outcome.review_status, PacketReviewStatus.PENDING)
        self.assertNotEqual(outcome.packet_sha256, self.stored.packet_sha256)
        # V52: the superseded approval no longer applies.
        self.assertIsNone(review_record_for(self.stored.packet_id, root=self.root))

    def test_failed_refresh_preserves_evidence_and_approval(self):
        client = FakeClient(search_error=ResearchTimeoutError("slow"))
        outcome = run_target(target(), client=client, refresh=True, root=self.root)

        self.assertIs(outcome.operation, TargetOperation.REFRESH)
        self.assertIs(outcome.outcome, TargetOutcome.FAILED)
        self.assertTrue(outcome.preserved_previous_packet)
        self.assertIs(outcome.failure_reason, rr.UnresolvedReason.RESEARCH_TIMEOUT)

        # V60: the valid frozen packet and its approval survive intact.
        packet = load_packet(self.stored.packet_id, root=self.root)
        self.assertIs(packet.status, PacketStatus.COMPLETE)
        self.assertEqual(packet.packet_sha256, self.stored.packet_sha256)
        record = review_record_for(self.stored.packet_id, root=self.root)
        self.assertIsNotNone(record)
        self.assertIs(record.status, PacketReviewStatus.APPROVED)

    def test_failed_refresh_is_a_failure_even_though_the_packet_survives(self):
        client = FakeClient(search_error=ResearchTimeoutError("slow"))
        results = [run_target(target(), client=client, refresh=True, root=self.root)]
        summary = summarize(results)
        self.assertEqual(summary.status, ResearchRunStatus.FAILED)
        self.assertEqual(summary.exit_code, 1)
        self.assertEqual(len(summary.refresh_failures), 1)


class TestRunnerPurity(unittest.TestCase):
    """V3 and V4, asserted against the source that implements them."""

    def setUp(self) -> None:
        self.source = Path(rr.__file__).read_text(encoding="utf-8")

    def test_no_database_or_config_imports(self):
        self.assertNotIn("from db", self.source)
        self.assertNotIn("import db", self.source)
        self.assertNotIn("from config", self.source)
        self.assertNotIn("psycopg", self.source)

    def test_no_opencodex_request_path(self):
        # V4: no provider client is imported, so no request path exists.
        self.assertNotIn("llm_categorizer", self.source)
        self.assertNotIn("categorizer", self.source)
        self.assertNotIn("OpenAI", self.source)

    def test_no_coverage_write_statements(self):
        lowered = self.source.lower()
        for statement in ("insert into", "update expenses", "delete from"):
            with self.subTest(statement=statement):
                self.assertNotIn(statement, lowered)

    def test_tinyfish_requests_are_confined_to_the_two_client_calls(self):
        # V3: only search() and fetch() reach the network.
        self.assertEqual(self.source.count("client.search("), 1)
        self.assertEqual(self.source.count("client.fetch("), 1)


class TestPostDiscoveryAuth(RunnerTestCase):
    """V55: auth failures after discovery are typed and persisted per target."""

    def test_auth_failure_after_discovery_is_persisted_as_auth(self):
        client = ok_client(
            results=(result(0, SEARCH_URL),),
            pages={},
            fetch_errors={SEARCH_URL: ResearchAuthError("denied")},
        )

        outcome = run_target(target(), client=client, root=self.root)

        self.assertIs(outcome.outcome, TargetOutcome.FAILED)
        self.assertIs(outcome.failure_reason, rr.UnresolvedReason.RESEARCH_AUTH)
        stored = load_packet(outcome.packet_id, root=self.root)
        self.assertIs(stored.status, PacketStatus.FAILED)
        self.assertIs(stored.failure_reason, rr.UnresolvedReason.RESEARCH_AUTH)

    def test_an_opened_circuit_carries_its_auth_cause_to_later_targets(self):
        class AuthThenCircuit(FakeClient):
            def search(self, derived_query: str):
                self.search_calls.append(derived_query)
                if self.circuit_open:
                    raise ResearchCircuitOpenError(
                        "circuit open", cause=self.circuit_reason
                    )
                if derived_query.startswith("beta"):
                    self.circuit_open = True
                    self.circuit_reason = rr.UnresolvedReason.RESEARCH_AUTH
                    raise ResearchAuthError("denied")
                return (result(0, SEARCH_URL),)

        client = AuthThenCircuit(pages={SEARCH_URL: page(SEARCH_URL)})
        targets = (
            target("acme widgets"),
            target("beta widgets"),
            target("gamma widgets"),
        )

        results = run_targets(targets, client=client, root=self.root)

        self.assertEqual(
            [item.outcome for item in results],
            [TargetOutcome.SUCCEEDED, TargetOutcome.FAILED, TargetOutcome.FAILED],
        )
        self.assertIs(results[1].failure_reason, rr.UnresolvedReason.RESEARCH_AUTH)
        # The circuit's first cause sticks, so the blocked target keeps it too.
        self.assertIs(results[2].failure_reason, rr.UnresolvedReason.RESEARCH_AUTH)
        self.assertIs(summarize(results).status, ResearchRunStatus.PARTIAL)
        # The earlier success must survive untouched.
        stored = load_packet(results[0].packet_id, root=self.root)
        self.assertIs(stored.status, PacketStatus.COMPLETE)


if __name__ == "__main__":
    unittest.main()

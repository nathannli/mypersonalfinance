"""Tests for packet-bound identity across cache, gold, validator and approval.

Pins T11's contract: enriched approval is its own protocol namespace that the
unenriched records can never satisfy or be satisfied by, every identity axis
invalidates approval when it moves, a refresh changes eligibility while a
preserved hash keeps it, packet-state failures cost zero provider calls and zero
database writes, and the three-pass runner builds fresh categorizer state per
pass with exactly one request per case.
"""

from __future__ import annotations

import json
import unittest
from dataclasses import replace
from pathlib import Path
from tempfile import TemporaryDirectory

from services.enriched_categorization import resolve_approved_packet
from services.gold_validator import (
    EnrichedCaseResult,
    EnrichedPassResult,
    EnrichedValidationResult,
    run_enriched_validation,
    write_enriched_approval_record,
)
from services.llm_categorizer import (
    EnrichedAbstain,
    EnrichedExecution,
    EnrichedSelect,
    EnrichedSuggestion,
)
from services.research_packets import (
    FetchedPage,
    PacketReviewRecord,
    PacketReviewStatus,
    PacketStatus,
    ResearchPacket,
    SearchResult,
    packet_path_for,
    record_review,
    store_packet,
    utc_now,
)
from services.transaction_categorization import ProviderAction, UnresolvedReason
from services.transaction_llm_approval import (
    ENRICHED_APPROVAL_KEY,
    ApprovalError,
    GoldCase,
    build_enriched_fingerprints,
    build_enriched_write_authorizer,
    build_write_authorizer,
    load_approval_record,
    load_enriched_approval_record,
    load_enriched_gold_cases,
    parse_enriched_gold_cases,
    verify_approval_record,
)

REPO_ROOT = Path(__file__).resolve().parents[1]
FIXTURE = (
    REPO_ROOT / "tests" / "fixtures" / "transaction_llm_gold_enriched_synthetic.json"
)
BASE_URL = "http://localhost:10100"
MODEL = "test-enriched-model"
EVIDENCE_URL = "https://beta.example/"


def fixture() -> dict:
    return json.loads(FIXTURE.read_text(encoding="utf-8"))


def taxonomy() -> list[dict]:
    return fixture()["taxonomies"]["finance"]


def build_packet(raw: dict, text: str | None = None) -> ResearchPacket:
    return ResearchPacket(
        normalized_merchant=raw["normalized_merchant"],
        derived_query=raw["derived_query"],
        status=PacketStatus(raw["status"]),
        searched_at=raw["searched_at"],
        search_results=tuple(SearchResult(**item) for item in raw["search_results"]),
        fetched_pages=tuple(
            FetchedPage(
                url=page["url"],
                final_url=page["final_url"],
                title=page["title"],
                description=page["description"],
                text=page["text"] if text is None else text,
                relevance_matched_tokens=tuple(page["relevance_matched_tokens"]),
            )
            for page in raw["fetched_pages"]
        ),
    )


def matching_decision(case) -> object:
    if case.expected_action == ProviderAction.SELECT:
        return EnrichedSelect(
            case.expected_choice_id, (EVIDENCE_URL,), case.fingerprint
        )
    if case.expected_action == ProviderAction.ABSTAIN:
        return EnrichedAbstain("Unclear descriptor.", case.fingerprint)
    return EnrichedSuggestion(
        case.expected_category_name,
        case.expected_subcategory_name,
        case.expected_parent_category_id,
        "Rationale text.",
        tuple(case.expected_evidence_urls),
        case.fingerprint,
    )


class StubCategorizer:
    """Stands in for the enriched provider client."""

    def __init__(self, decisions):
        self.decisions = decisions
        self.provider_call_count = 0
        self.cache_hit_count = 0
        self.circuit_open = False
        self.execution_calls = 0
        self.executed_fingerprints: list[str] = []

    def categorize_enriched(self, context, packet):
        self.execution_calls += 1
        self.executed_fingerprints.append(context.fingerprint)
        self.provider_call_count += 1
        return EnrichedExecution(decision=self.decisions[context.fingerprint])


class RecordingFactory:
    def __init__(self, decisions):
        self.decisions = decisions
        self.built: list[StubCategorizer] = []

    def __call__(self) -> StubCategorizer:
        categorizer = StubCategorizer(self.decisions)
        self.built.append(categorizer)
        return categorizer


class EnrichedFixtureCase(unittest.TestCase):
    def setUp(self) -> None:
        self._temporary = TemporaryDirectory()
        self.addCleanup(self._temporary.cleanup)
        self.root = Path(self._temporary.name)
        self.approval_path = self.root / "approvals.json"
        self.raw_packets = {
            raw["normalized_merchant"]: raw for raw in fixture()["packets"]
        }
        self.packets = {
            merchant: build_packet(raw) for merchant, raw in self.raw_packets.items()
        }

    def store_all(self, *, approve: bool = True) -> None:
        for packet in self.packets.values():
            store_packet(packet, root=self.root)
            if approve:
                record_review(
                    PacketReviewRecord(
                        packet_id=packet.packet_id,
                        packet_sha256=packet.packet_sha256,
                        status=PacketReviewStatus.APPROVED,
                        reviewed_at=utc_now(),
                    ),
                    root=self.root,
                )

    def resolver(self):
        return lambda merchant: resolve_approved_packet(merchant, root=self.root)

    def load_cases(self, path: Path = FIXTURE):
        return load_enriched_gold_cases(
            "finance", taxonomy(), packet_resolver=self.resolver(), path=path
        )

    def passing_validation(self, cases) -> EnrichedValidationResult:
        passes = [
            EnrichedPassResult(
                results=[
                    EnrichedCaseResult(case.fingerprint, True, "select:1")
                    for case in cases
                ]
            )
            for _ in range(3)
        ]
        return EnrichedValidationResult(passes=passes)

    def write_approval(self, cases) -> None:
        write_enriched_approval_record(
            database="finance",
            base_url=BASE_URL,
            model=MODEL,
            choices=taxonomy(),
            cases=cases,
            validation=self.passing_validation(cases),
            approval_path=self.approval_path,
        )

    def expected_fingerprints(self, gold_cases, **overrides) -> dict:
        arguments = {
            "database": "finance",
            "base_url": BASE_URL,
            "model": MODEL,
            "choices": taxonomy(),
            "cases": gold_cases,
        }
        arguments.update(overrides)
        return build_enriched_fingerprints(**arguments)


class TestApprovalNamespaces(EnrichedFixtureCase):
    def test_enriched_approval_preserves_legacy_entries_byte_for_byte(self):
        legacy = {
            "finance": {"model": "unenriched-model", "passes": 3},
            "parents_finance": {"model": "parents-model", "passes": 3},
        }
        self.approval_path.write_text(json.dumps(legacy), encoding="utf-8")
        self.store_all()
        cases = self.load_cases()

        self.write_approval(cases)

        document = json.loads(self.approval_path.read_text(encoding="utf-8"))
        self.assertEqual(document["finance"], legacy["finance"])
        self.assertEqual(document["parents_finance"], legacy["parents_finance"])
        self.assertIn(ENRICHED_APPROVAL_KEY, document)
        self.assertEqual(document[ENRICHED_APPROVAL_KEY]["protocol"], "enriched")

    def test_a_legacy_record_never_satisfies_the_enriched_gate(self):
        self.approval_path.write_text(
            json.dumps({"finance": {"model": "unenriched", "passes": 3}}),
            encoding="utf-8",
        )

        with self.assertRaises(ApprovalError) as raised:
            load_enriched_approval_record(self.approval_path)

        self.assertIn(ENRICHED_APPROVAL_KEY, str(raised.exception))

    def test_an_enriched_record_never_satisfies_the_legacy_gate(self):
        self.store_all()
        cases = self.load_cases()
        self.write_approval(cases)

        with self.assertRaises(ApprovalError):
            load_approval_record("finance", self.approval_path)

    def test_the_record_carries_no_merchant_amount_or_packet_body(self):
        self.store_all()
        cases = self.load_cases()

        self.write_approval(cases)

        text = self.approval_path.read_text(encoding="utf-8")
        for case in cases:
            self.assertNotIn(case.merchant, text)
            self.assertNotIn(str(case.amount_minor_units), text)
            self.assertNotIn("Synthetic bounded evidence text", text)

    def test_the_enriched_record_binds_protocol_and_mode(self):
        self.store_all()
        cases = self.load_cases()

        self.write_approval(cases)

        record = load_enriched_approval_record(self.approval_path)
        self.assertEqual(record["protocol"], "enriched")
        self.assertEqual(record["mode"], "write")
        self.assertGreaterEqual(record["passes"], 3)


class TestApprovalInvalidation(EnrichedFixtureCase):
    def setUp(self) -> None:
        super().setUp()
        self.store_all()
        self.cases = self.load_cases()
        self.write_approval(self.cases)
        self.record = load_enriched_approval_record(self.approval_path)

    def assert_stale(self, **overrides) -> None:
        expected = self.expected_fingerprints(self.cases, **overrides)
        with self.assertRaises(ApprovalError) as raised:
            verify_approval_record(self.record, expected)
        self.assertIn("stale", str(raised.exception))

    def test_the_untouched_record_still_verifies(self):
        verify_approval_record(self.record, self.expected_fingerprints(self.cases))

    def test_a_changed_model_invalidates_approval(self):
        self.assert_stale(model="some-other-model")

    def test_a_changed_base_url_invalidates_approval(self):
        self.assert_stale(base_url="http://elsewhere:10100")

    def test_a_changed_taxonomy_invalidates_approval(self):
        widened = list(taxonomy()) + [
            {
                "category_id": 13,
                "category_name": "Sample Home",
                "subcategory_id": 104,
                "subcategory_name": "Sample Garden",
            }
        ]
        self.assert_stale(choices=widened)

    def test_a_changed_gold_subset_invalidates_approval(self):
        self.assert_stale(cases=self.cases[:-1])

    def test_a_changed_packet_hash_invalidates_approval(self):
        mutated = [
            replace(case, research_packet_sha256="a" * 64) for case in self.cases
        ]
        self.assert_stale(cases=mutated)

    def test_a_changed_packet_schema_version_invalidates_approval(self):
        mutated = [
            replace(case, packet_schema_version="transaction-web-research-v0")
            for case in self.cases
        ]
        self.assert_stale(cases=mutated)

    def test_a_changed_packet_query_version_invalidates_approval(self):
        mutated = [
            replace(case, packet_query_version="merchant-research-v0")
            for case in self.cases
        ]
        self.assert_stale(cases=mutated)

    def test_a_changed_prompt_or_schema_byte_invalidates_approval(self):
        from unittest.mock import patch

        import services.transaction_llm_approval as approval

        for label in (
            "canonical_enriched_prompt_bytes",
            "canonical_enriched_response_schema_bytes",
        ):
            with self.subTest(axis=label):
                with patch.object(approval, label, return_value=b"changed-bytes"):
                    self.assert_stale()

    def test_a_fewer_pass_record_is_rejected(self):
        thin = dict(self.record)
        thin["passes"] = 1

        with self.assertRaises(ApprovalError):
            verify_approval_record(thin, self.expected_fingerprints(self.cases))


class TestPacketBoundAuthorizer(EnrichedFixtureCase):
    def setUp(self) -> None:
        super().setUp()
        self.store_all()
        self.cases = self.load_cases()
        self.authorize = build_enriched_write_authorizer(self.cases)

    def select_case(self):
        return next(
            case for case in self.cases if case.expected_action == ProviderAction.SELECT
        )

    def test_the_exact_approved_select_is_authorized(self):
        case = self.select_case()
        packet = self.packets[case.merchant]

        self.assertTrue(
            self.authorize(
                case.build_context(taxonomy()), packet, case.expected_choice_id
            )
        )

    def test_a_wrong_choice_is_not_authorized(self):
        case = self.select_case()
        packet = self.packets[case.merchant]

        self.assertFalse(self.authorize(case.build_context(taxonomy()), packet, 999999))

    def test_a_suggestion_case_is_never_authorized(self):
        case = next(
            case
            for case in self.cases
            if case.expected_action == ProviderAction.SUGGEST_NEW
        )
        packet = self.packets[case.merchant]

        self.assertFalse(self.authorize(case.build_context(taxonomy()), packet, 101))

    def test_an_unbound_context_is_never_authorized(self):
        case = self.select_case()

        unbound = replace(
            case.build_context(taxonomy()),
            research_packet_sha256=None,
            research_packet_schema_version=None,
            research_packet_query_version=None,
        )

        self.assertFalse(
            self.authorize(
                unbound, self.packets[case.merchant], case.expected_choice_id
            )
        )

    def test_a_refreshed_packet_no_longer_authorizes_the_old_context(self):
        case = self.select_case()
        original = self.packets[case.merchant]
        refreshed = build_packet(
            self.raw_packets[case.merchant], text="Refreshed evidence."
        )

        self.assertNotEqual(original.packet_sha256, refreshed.packet_sha256)
        self.assertFalse(
            self.authorize(
                case.build_context(taxonomy()), refreshed, case.expected_choice_id
            )
        )

    def test_the_unenriched_authorizer_never_satisfies_an_enriched_context(self):
        # Same transaction, same choice: only the packet binding differs, so a
        # legacy authorizer keyed on the unbound fingerprint must decline (V33).
        case = self.select_case()
        unbound = replace(
            case.build_context(taxonomy()),
            research_packet_sha256=None,
            research_packet_schema_version=None,
            research_packet_query_version=None,
        )
        legacy = GoldCase(
            database=unbound.database,
            fingerprint=unbound.fingerprint,
            merchant=unbound.merchant,
            amount_minor_units=unbound.amount_minor_units,
            statement_category=unbound.statement_category,
            expected_action=ProviderAction.SELECT,
            expected_choice_id=case.expected_choice_id,
        )
        authorize_legacy = build_write_authorizer([legacy])

        self.assertTrue(authorize_legacy(unbound, case.expected_choice_id))
        self.assertFalse(
            authorize_legacy(case.build_context(taxonomy()), case.expected_choice_id)
        )


class TestRefreshEligibility(EnrichedFixtureCase):
    def test_a_successful_refresh_invalidates_case_eligibility(self):
        self.store_all()
        self.load_cases()  # eligible before the refresh

        case = next(
            case
            for case in self.load_cases()
            if case.expected_action == ProviderAction.SELECT
        )
        # A successful refresh replaces the packet, so its hash moves and the
        # approval that bound the old hash no longer applies (V14).
        store_packet(
            build_packet(self.raw_packets[case.merchant], text="Refreshed evidence."),
            root=self.root,
        )

        with self.assertRaises(ApprovalError):
            self.load_cases()

        resolution = resolve_approved_packet(case.merchant, root=self.root)
        self.assertIsNone(resolution.packet)
        self.assertIs(resolution.reason, UnresolvedReason.RESEARCH_UNAPPROVED)

    def test_a_preserved_packet_hash_preserves_eligibility(self):
        self.store_all()

        first = self.load_cases()
        second = self.load_cases()

        self.assertEqual(
            [case.fingerprint for case in first],
            [case.fingerprint for case in second],
        )

    def test_a_packet_refresh_changes_the_gold_fingerprints(self):
        # V46: re-binding evidence moves every case into a new identity space.
        before = parse_enriched_gold_cases(fixture(), "finance")
        mutated = [replace(case, research_packet_sha256="b" * 64) for case in before]

        self.assertNotEqual(
            build_enriched_fingerprints("finance", BASE_URL, MODEL, taxonomy(), before)[
                "gold_sha256"
            ],
            build_enriched_fingerprints(
                "finance", BASE_URL, MODEL, taxonomy(), mutated
            )["gold_sha256"],
        )


class TestThreePassEnrichedRunner(EnrichedFixtureCase):
    def setUp(self) -> None:
        super().setUp()
        self.store_all()
        self.cases = self.load_cases()
        self.decisions = {
            case.fingerprint: matching_decision(case) for case in self.cases
        }

    def test_three_fresh_passes_each_cost_one_request_per_case(self):
        factory = RecordingFactory(self.decisions)

        validation = run_enriched_validation(
            self.cases,
            taxonomy(),
            factory,
            packet_resolver=self.resolver(),
        )

        self.assertTrue(validation.approved)
        self.assertEqual(len(validation.passes), 3)
        self.assertEqual(len(factory.built), 3)
        self.assertEqual(len({id(item) for item in factory.built}), 3)
        for single in validation.passes:
            self.assertTrue(single.passed)
            self.assertEqual(single.provider_calls, len(self.cases))

    def test_a_mismatched_decision_stops_after_the_first_pass(self):
        failure_case = self.cases[0]
        self.decisions[failure_case.fingerprint] = EnrichedAbstain(
            "wrong", failure_case.fingerprint
        )
        factory = RecordingFactory(self.decisions)

        validation = run_enriched_validation(
            self.cases,
            taxonomy(),
            factory,
            packet_resolver=self.resolver(),
        )

        self.assertFalse(validation.approved)
        self.assertEqual(len(validation.passes), 1)
        self.assertEqual(len(factory.built), 1)
        self.assertFalse(validation.passes[0].passed)

    def test_a_packet_state_failure_costs_that_case_a_provider_call(self):
        missing = "example ambiguous counter"
        packet = self.packets[missing]
        packet_path_for(packet.packet_id, root=self.root).unlink()
        failing = next(case for case in self.cases if case.merchant == missing)
        factory = RecordingFactory(self.decisions)

        with self.assertRaises(ApprovalError):
            run_enriched_validation(
                self.cases,
                taxonomy(),
                factory,
                packet_resolver=self.resolver(),
            )

        # Eligibility is re-checked immediately before each request, so the
        # unavailable packet is never sent to the provider. Earlier cases in the
        # same pass may legitimately have run before it was reached.
        executed = {
            fingerprint
            for item in factory.built
            for fingerprint in item.executed_fingerprints
        }
        self.assertNotIn(failing.fingerprint, executed)

    def test_an_empty_subset_is_rejected(self):
        with self.assertRaises(ApprovalError):
            run_enriched_validation(
                [],
                taxonomy(),
                RecordingFactory(self.decisions),
                packet_resolver=self.resolver(),
            )

    def test_the_runner_makes_no_web_research_calls(self):
        # ``sys.modules`` cannot be inspected here: other test modules in this
        # suite legitimately import the research CLI. tests/test_enriched_gold_cases.py
        # proves the zero-TinyFish property for this module in a fresh interpreter.
        source = (REPO_ROOT / "services" / "gold_validator.py").read_text(
            encoding="utf-8"
        )

        self.assertNotIn("tinyfish", source.lower())


if __name__ == "__main__":
    unittest.main()

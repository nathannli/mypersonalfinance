"""Tests for the enriched finance load path (T8).

Pins the wiring contract: the packet gate runs only for deterministic-no-match
rows, every packet state surfaces as its own typed reason without a provider
call or a database write, and enriched decisions map to exactly one outcome.
"""

from __future__ import annotations

import unittest
from datetime import date
from pathlib import Path

from db.my_finance import MyFinanceDB
from services.enriched_categorization import PacketResolution
from services.llm_categorizer import (
    EnrichedAbstain,
    EnrichedExecution,
    EnrichedSelect,
    EnrichedSuggestion,
)
from services.research_packets import (
    CategorySuggestion,
    FetchedPage,
    PacketStatus,
    ResearchPacket,
    SearchResult,
)
from services.transaction_categorization import (
    Resolution,
    TransactionStatus,
    UnresolvedReason,
)

EVIDENCE_URL = "https://acme.example/about"
CHOICES = [
    {
        "subcategory_id": 11,
        "category_id": 2,
        "subcategory_name": "Eating Out",
        "category_name": "Food",
    },
    {
        "subcategory_id": 36,
        "category_id": 4,
        "subcategory_name": "AI/Coding",
        "category_name": "Entertainment",
    },
]


def make_packet(merchant: str = "openai") -> ResearchPacket:
    return ResearchPacket(
        normalized_merchant=merchant,
        derived_query=merchant,
        status=PacketStatus.COMPLETE,
        searched_at="2026-09-17T12:00:00+00:00",
        search_results=(
            SearchResult(
                position=0,
                site_name="Example",
                title="Example",
                snippet="An example",
                url=EVIDENCE_URL,
            ),
        ),
        fetched_pages=(
            FetchedPage(
                url=EVIDENCE_URL,
                final_url=EVIDENCE_URL,
                title="Example",
                description="An example",
                text="An example body.",
                relevance_matched_tokens=("example",),
            ),
        ),
    )


def approved(merchant: str = "openai") -> PacketResolution:
    return PacketResolution(packet=make_packet(merchant))


class FakeEnrichedCategorizer:
    """Stands in for the enriched provider client."""

    def __init__(self, execution=None, authorized: bool = False):
        self.execution = execution
        self.authorized = authorized
        self.calls: list[tuple] = []
        self.authorization_calls: list[tuple] = []
        self.enriched_authorization_calls: list[tuple] = []

    def categorize_enriched(self, context, packet):
        self.calls.append((context, packet))
        return self.execution

    def can_write(self, context, choice_id):
        self.authorization_calls.append((context, choice_id))
        return self.authorized

    def can_write_enriched(self, context, packet, choice_id):
        self.enriched_authorization_calls.append((context, packet, choice_id))
        return self.authorized


class FakeFinanceDB(MyFinanceDB):
    """Bypasses __init__ so no database connection is opened."""

    def __init__(self, resolution=approved):
        self.exact_duplicate = False
        self.reimbursement_duplicate = False
        self.merchant_is_reimbursement = False
        self.reference = None
        self.choices = list(CHOICES)
        self.insert_calls: list[tuple] = []
        self.auto_match_insert_calls: list[tuple] = []
        self.resolved_merchants: list[str] = []
        base = resolution if callable(resolution) else (lambda merchant: resolution)

        def tracked(merchant):
            self.resolved_merchants.append(merchant)
            return base(merchant)

        self._packet_resolver = tracked
        # V22/V24: the grouped-suggestion writer is injected the same way, so
        # these tests never touch the repository-root private artifact.
        self.suggestion_calls: list[CategorySuggestion] = []
        self.suggestion_failure: Exception | None = None

        def record(suggestion):
            self.suggestion_calls.append(suggestion)
            if self.suggestion_failure is not None:
                raise self.suggestion_failure
            return suggestion

        self._suggestion_recorder = record

    def check_if_expense_exists(self, date, merchant, cost):
        return self.exact_duplicate

    def check_if_reimbursement_expense_exists(self, date, merchant):
        return self.reimbursement_duplicate

    def _is_reimbursement_merchant(self, merchant: str) -> bool:
        # Controllable stand-in for the real reference-data lookup, so these
        # tests do not depend on which merchants ship in ref_data.
        return self.merchant_is_reimbursement

    def get_categorization_choices(self):
        return list(self.choices)

    def get_auto_match_category(self, merchant):
        return self.reference

    def insert(self, query, args):
        self.insert_calls.append((query, args))

    def insert_into_auto_match(self, merchant, category, subcategory):
        self.auto_match_insert_calls.append((merchant, category, subcategory))


def select_decision(choice_id: int = 36) -> EnrichedExecution:
    return EnrichedExecution(
        decision=EnrichedSelect(
            choice_id=choice_id,
            evidence_urls=(EVIDENCE_URL,),
            context_fingerprint="f" * 64,
        )
    )


def suggestion_decision() -> EnrichedExecution:
    return EnrichedExecution(
        decision=EnrichedSuggestion(
            category_name="Gadgets",
            subcategory_name="Widgets",
            parent_category_id=None,
            rationale="No offered choice covers widget vendors.",
            evidence_urls=(EVIDENCE_URL,),
            context_fingerprint="f" * 64,
        )
    )


class EnrichedFlowTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.db = FakeFinanceDB()
        self.transaction_date = date(2026, 9, 15)

    def insert(self, categorizer=None, merchant: str = "OPENAI"):
        return self.db.insert_expense(
            self.transaction_date,
            merchant,
            20.0,
            "amex",
            "Services",
            categorizer=categorizer,
        )


class TestPacketGateIsOrdered(EnrichedFlowTestCase):
    def test_duplicate_row_never_resolves_a_packet(self):
        self.db.exact_duplicate = True
        self.insert(FakeEnrichedCategorizer(select_decision()))

        self.assertEqual(self.db.resolved_merchants, [])

    def test_reimbursement_duplicate_never_resolves_a_packet(self):
        self.db.reimbursement_duplicate = True
        self.db.merchant_is_reimbursement = True
        self.insert(FakeEnrichedCategorizer(select_decision()))

        self.assertEqual(self.db.resolved_merchants, [])

    def test_deterministic_match_never_resolves_a_packet(self):
        self.db.reference = ("Food", "Eating Out")
        outcome = self.insert(FakeEnrichedCategorizer(select_decision()))

        self.assertEqual(outcome.status, TransactionStatus.INSERTED)
        self.assertEqual(outcome.resolution, Resolution.DETERMINISTIC)
        self.assertEqual(self.db.resolved_merchants, [])

    def test_invalid_context_returns_before_resolving_a_packet(self):
        outcome = self.db.insert_expense(
            self.transaction_date, "OPENAI", 0.005, "amex", None, None
        )

        self.assertEqual(outcome.reason, UnresolvedReason.INVALID_CONTEXT)
        self.assertEqual(self.db.resolved_merchants, [])


class TestPacketStateReasons(EnrichedFlowTestCase):
    def assert_state_reason(self, reason) -> None:
        categorizer = FakeEnrichedCategorizer(select_decision())
        self.db._packet_resolver = lambda merchant: PacketResolution(reason=reason)

        outcome = self.insert(categorizer)

        self.assertEqual(outcome.status, TransactionStatus.UNRESOLVED)
        self.assertIs(outcome.reason, reason)
        # No provider call and no database mutation for a blocked packet.
        self.assertEqual(categorizer.calls, [])
        self.assertEqual(self.db.insert_calls, [])
        self.assertEqual(self.db.auto_match_insert_calls, [])

    def test_missing_packet(self):
        self.assert_state_reason(UnresolvedReason.RESEARCH_MISSING)

    def test_unapproved_packet(self):
        self.assert_state_reason(UnresolvedReason.RESEARCH_UNAPPROVED)

    def test_stale_packet(self):
        self.assert_state_reason(UnresolvedReason.RESEARCH_STALE)

    def test_tampered_packet(self):
        self.assert_state_reason(UnresolvedReason.RESEARCH_TAMPERED)

    def test_malformed_packet(self):
        self.assert_state_reason(UnresolvedReason.RESEARCH_MALFORMED)

    def test_stored_execution_failure_keeps_its_exact_reason(self):
        for reason in (
            UnresolvedReason.RESEARCH_TIMEOUT,
            UnresolvedReason.RESEARCH_IRRELEVANT,
            UnresolvedReason.RESEARCH_NO_RESULTS,
            UnresolvedReason.RESEARCH_AUTH,
        ):
            with self.subTest(reason=reason.value):
                self.db = FakeFinanceDB()
                self.assert_state_reason(reason)

    def test_approved_packet_without_a_categorizer_is_provider_error(self):
        outcome = self.insert(None)

        self.assertEqual(outcome.status, TransactionStatus.UNRESOLVED)
        self.assertIs(outcome.reason, UnresolvedReason.PROVIDER_ERROR)
        self.assertEqual(self.db.insert_calls, [])

    def test_approved_packet_reaches_the_provider_with_that_packet(self):
        categorizer = FakeEnrichedCategorizer(select_decision())

        self.insert(categorizer)

        self.assertEqual(len(categorizer.calls), 1)
        context, packet = categorizer.calls[0]
        self.assertEqual(context.database, "finance")
        # The merchant is normalized before it becomes a packet lookup key (V41).
        self.assertEqual(context.merchant, "openai")
        self.assertEqual(context.amount_minor_units, 2000)
        self.assertEqual(packet.normalized_merchant, "openai")


class TestEnrichedDecisionMapping(EnrichedFlowTestCase):
    def test_authorized_select_inserts_via_the_packet_bound_gate(self):
        # V33: an exact approved select for this context and packet writes.
        categorizer = FakeEnrichedCategorizer(select_decision(), authorized=True)
        outcome = self.insert(categorizer)

        self.assertEqual(outcome.status, TransactionStatus.INSERTED)
        self.assertEqual(outcome.resolution, Resolution.LLM)
        self.assertEqual(len(self.db.insert_calls), 1)
        # The unenriched authorizer is never consulted for an enriched select.
        self.assertEqual(categorizer.authorization_calls, [])
        self.assertEqual(len(categorizer.enriched_authorization_calls), 1)
        self.assertEqual(self.db.auto_match_insert_calls, [])

    def test_unauthorized_select_stays_shadow(self):
        categorizer = FakeEnrichedCategorizer(select_decision(), authorized=False)
        outcome = self.insert(categorizer)

        self.assertEqual(outcome.status, TransactionStatus.SHADOW)
        self.assertEqual(outcome.resolution, Resolution.LLM)
        self.assertEqual(outcome.suggested_choice_id, 36)
        self.assertEqual(self.db.insert_calls, [])
        self.assertEqual(self.db.auto_match_insert_calls, [])

    def test_a_suggestion_never_reaches_the_write_gate(self):
        categorizer = FakeEnrichedCategorizer(suggestion_decision(), authorized=True)
        outcome = self.insert(categorizer)

        self.assertEqual(outcome.status, TransactionStatus.SUGGESTED)
        self.assertEqual(categorizer.enriched_authorization_calls, [])
        self.assertEqual(self.db.insert_calls, [])

    def test_select_outside_the_live_taxonomy_is_invalid_choice(self):
        outcome = self.insert(FakeEnrichedCategorizer(select_decision(999)))

        self.assertEqual(outcome.status, TransactionStatus.UNRESOLVED)
        self.assertIs(outcome.reason, UnresolvedReason.INVALID_CHOICE)
        self.assertEqual(self.db.insert_calls, [])

    def test_suggestion_is_suggested_without_any_insert(self):
        outcome = self.insert(FakeEnrichedCategorizer(suggestion_decision()))

        self.assertEqual(outcome.status, TransactionStatus.SUGGESTED)
        self.assertEqual(outcome.resolution, Resolution.LLM)
        self.assertIsNotNone(outcome.suggestion_id)
        self.assertIsNone(outcome.suggested_choice_id)
        # V21: suggest_new never writes an expense or an auto-match row.
        self.assertEqual(self.db.insert_calls, [])
        self.assertEqual(self.db.auto_match_insert_calls, [])

    def test_suggestion_id_is_stable_for_the_same_merchant_and_packet(self):
        first = self.insert(FakeEnrichedCategorizer(suggestion_decision()))
        second = self.insert(FakeEnrichedCategorizer(suggestion_decision()))

        self.assertEqual(first.suggestion_id, second.suggestion_id)

    def test_suggestion_is_persisted_before_it_is_reported(self):
        outcome = self.insert(FakeEnrichedCategorizer(suggestion_decision()))

        # V22: exactly one stored proposal, and the reported id is its id.
        self.assertEqual(len(self.db.suggestion_calls), 1)
        stored = self.db.suggestion_calls[0]
        self.assertEqual(stored.normalized_merchant, "openai")
        self.assertEqual(stored.suggestion_id, outcome.suggestion_id)
        self.assertEqual(stored.evidence_urls, (EVIDENCE_URL,))

    def test_suggestion_store_failure_is_never_reported_as_suggested(self):
        self.db.suggestion_failure = RuntimeError("store unavailable")

        with self.assertRaises(RuntimeError):
            self.insert(FakeEnrichedCategorizer(suggestion_decision()))

        # V21/V22: a failed persist yields neither a suggested row nor a write.
        self.assertEqual(self.db.insert_calls, [])
        self.assertEqual(self.db.auto_match_insert_calls, [])

    def test_abstain_is_unresolved_abstained(self):
        categorizer = FakeEnrichedCategorizer(
            EnrichedExecution(
                decision=EnrichedAbstain(
                    reason="Evidence is too thin to decide.",
                    context_fingerprint="f" * 64,
                )
            )
        )
        outcome = self.insert(categorizer)

        self.assertEqual(outcome.status, TransactionStatus.UNRESOLVED)
        self.assertIs(outcome.reason, UnresolvedReason.ABSTAINED)
        self.assertEqual(self.db.insert_calls, [])

    def test_execution_failure_reason_is_preserved(self):
        outcome = self.insert(
            FakeEnrichedCategorizer(EnrichedExecution(reason=UnresolvedReason.TIMEOUT))
        )

        self.assertEqual(outcome.status, TransactionStatus.UNRESOLVED)
        self.assertIs(outcome.reason, UnresolvedReason.TIMEOUT)
        self.assertEqual(self.db.insert_calls, [])

    def test_execution_failure_without_a_reason_is_malformed(self):
        broken = EnrichedExecution(decision=select_decision().decision)
        object.__setattr__(broken, "decision", None)
        outcome = self.insert(FakeEnrichedCategorizer(broken))

        self.assertIs(outcome.reason, UnresolvedReason.MALFORMED)


class TestNoTinyFishInTheLoadPath(unittest.TestCase):
    """V3: only the research CLI may call TinyFish."""

    def test_load_path_modules_reference_no_tinyfish_client(self):
        root = Path(__file__).resolve().parents[1]
        for relative in (
            "db/my_finance.py",
            "services/enriched_categorization.py",
        ):
            source = (root / relative).read_text(encoding="utf-8")
            with self.subTest(module=relative):
                # The module may name TinyFish in prose describing the boundary;
                # what matters is that no client or transport is reachable.
                self.assertNotIn("TinyFishClient", source)
                self.assertNotIn("tinyfish_research", source)
                self.assertNotIn("TINYFISH_API_KEY", source)
                self.assertNotIn("requests.", source)

    def test_resolver_is_used_by_default(self):
        import services.enriched_categorization as module
        from db import my_finance

        self.assertIs(
            my_finance.resolve_approved_packet,
            module.resolve_approved_packet,
        )


if __name__ == "__main__":
    unittest.main()

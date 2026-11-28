import unittest
from datetime import date
from unittest.mock import patch

import polars as pl

from db.parents_finance import ParentsFinanceDB
from services.enriched_categorization import PacketResolution
from services.llm_categorizer import (
    EnrichedAbstain,
    EnrichedExecution,
    EnrichedSelect,
    EnrichedSuggestion,
)
from services.research_packets import (
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


CATEGORY_ROWS = [
    (1, "Food"),
    (2, "Utilities"),
    (3, "Ignore"),
    (4, "Transportation"),
]


def make_packet(merchant: str) -> ResearchPacket:
    return ResearchPacket(
        normalized_merchant=merchant,
        derived_query=merchant,
        status=PacketStatus.COMPLETE,
        searched_at="2026-09-17T12:00:00+00:00",
        search_results=(
            SearchResult(
                position=0,
                site_name="Acme",
                title="Acme",
                snippet="s",
                url=EVIDENCE_URL,
            ),
        ),
        fetched_pages=(
            FetchedPage(
                url=EVIDENCE_URL,
                final_url=EVIDENCE_URL,
                title="Acme",
                description="d",
                text="t",
                relevance_matched_tokens=("acme",),
            ),
        ),
    )


class FakeCategorizer:
    def __init__(self, execution=None, authorized=False):
        self.execution = execution or select_result(1)
        self.authorized = authorized
        self.calls = []
        self.authorization_calls = []

    @property
    def contexts(self):
        return [context for context, _ in self.calls]

    def categorize_enriched(self, context, packet):
        self.calls.append((context, packet))
        return self.execution

    def can_write_enriched(self, context, packet, choice_id):
        self.authorization_calls.append((context, packet, choice_id))
        return self.authorized


class FakeParentsFinanceDB(ParentsFinanceDB):
    auto_match: str | None
    auto_match_error: ValueError | None

    def __init__(self, cron: bool = False):
        self.cron = cron
        self.exact_duplicate = False
        self.auto_match = None
        self.auto_match_error = None
        self.category_rows = list(CATEGORY_ROWS)
        self.insert_calls = []
        self.packet_reason = None
        self.resolved_merchants = []
        self.suggestions = []
        self._packet_resolver = self.resolve_packet
        self._suggestion_recorder = self.record

    def resolve_packet(self, merchant):
        self.resolved_merchants.append(merchant)
        if self.packet_reason is not None:
            return PacketResolution(reason=self.packet_reason)
        return PacketResolution(packet=make_packet(merchant))

    def record(self, suggestion):
        self.suggestions.append(suggestion)
        return suggestion

    def check_if_expense_exists(self, date, merchant, cost):
        return self.exact_duplicate

    def get_category(self):
        return pl.DataFrame(
            self.category_rows,
            schema={"id": pl.Int64, "category": pl.Utf8},
            orient="row",
        ).sort("category")

    def get_auto_match_category(self, merchant):
        if self.auto_match_error is not None:
            raise self.auto_match_error
        return self.auto_match

    def insert(self, query, args):
        self.insert_calls.append((query, args))


def select_result(choice_id):
    return EnrichedExecution(
        decision=EnrichedSelect(choice_id, (EVIDENCE_URL,), "f" * 64)
    )


class ParentsFlowTestCase(unittest.TestCase):
    def setUp(self):
        self.db = FakeParentsFinanceDB()
        self.transaction_date = date(2026, 9, 15)

    def insert(self, categorizer=None, cc_category=None, merchant="HYDRO ONE"):
        return self.db.insert_expense(
            self.transaction_date,
            merchant,
            41.5,
            "",
            cc_category,
            categorizer=categorizer,
        )


class TestParentsDeterministicFlow(ParentsFlowTestCase):
    def test_exact_duplicate_returns_before_matching_or_llm(self):
        self.db.exact_duplicate = True
        categorizer = FakeCategorizer(select_result(2))

        outcome = self.insert(categorizer=categorizer, cc_category="Utilities")

        self.assertEqual(outcome.status, TransactionStatus.DUPLICATE)
        self.assertEqual(outcome.resolution, Resolution.NONE)
        self.assertEqual(categorizer.contexts, [])
        self.assertEqual(self.db.insert_calls, [])

    def test_statement_category_resolves_without_llm(self):
        categorizer = FakeCategorizer(select_result(1))

        with patch("builtins.input", side_effect=AssertionError("prompted")):
            outcome = self.insert(categorizer=categorizer, cc_category="Utilities\xa0")

        self.assertEqual(outcome.status, TransactionStatus.INSERTED)
        self.assertEqual(outcome.resolution, Resolution.DETERMINISTIC)
        self.assertEqual(categorizer.contexts, [])
        self.assertEqual(self.db.insert_calls[0][1][-1], 2)

    def test_merchant_auto_match_resolves_when_statement_category_missing(self):
        self.db.auto_match = "Transportation"

        outcome = self.insert()

        self.assertEqual(outcome.status, TransactionStatus.INSERTED)
        self.assertEqual(outcome.resolution, Resolution.DETERMINISTIC)
        self.assertEqual(self.db.insert_calls[0][1][-1], 4)

    def test_ignore_category_is_typed_ignored_without_insert(self):
        outcome = self.insert(cc_category="Ignore")

        self.assertEqual(outcome.status, TransactionStatus.IGNORED)
        self.assertEqual(outcome.resolution, Resolution.DETERMINISTIC)
        self.assertEqual(self.db.insert_calls, [])

    def test_ambiguous_auto_match_is_unresolved_without_llm(self):
        self.db.auto_match_error = ValueError("multiple matches")
        categorizer = FakeCategorizer(select_result(1))

        outcome = self.insert(categorizer=categorizer)

        self.assertEqual(outcome.status, TransactionStatus.UNRESOLVED)
        self.assertEqual(outcome.resolution, Resolution.DETERMINISTIC)
        self.assertEqual(outcome.reason, UnresolvedReason.INVALID_CHOICE)
        self.assertEqual(categorizer.contexts, [])

    def test_stale_auto_match_is_unresolved_without_llm(self):
        # V44: a stale mapping is surfaced deterministically in both
        # databases; it never silently reroutes to the LLM path.
        self.db.auto_match = "Retired Category"
        categorizer = FakeCategorizer(select_result(1))

        outcome = self.insert(categorizer=categorizer)

        self.assertEqual(outcome.status, TransactionStatus.UNRESOLVED)
        self.assertEqual(outcome.resolution, Resolution.DETERMINISTIC)
        self.assertEqual(outcome.reason, UnresolvedReason.INVALID_CHOICE)
        self.assertEqual(categorizer.contexts, [])
        self.assertEqual(self.db.insert_calls, [])


class TestParentsLlmFlow(ParentsFlowTestCase):
    def test_unknown_merchant_stays_shadow_without_authorization(self):
        categorizer = FakeCategorizer(select_result(1), authorized=False)

        with patch("builtins.input", side_effect=AssertionError("prompted")):
            outcome = self.insert(categorizer=categorizer)

        self.assertEqual(outcome.status, TransactionStatus.SHADOW)
        self.assertEqual(outcome.resolution, Resolution.LLM)
        self.assertEqual(outcome.suggested_choice_id, 1)
        self.assertEqual(self.db.insert_calls, [])
        ((context, packet),) = categorizer.calls
        self.assertEqual(context.database, "parents_finance")
        self.assertEqual(context.amount_minor_units, 4150)
        self.assertEqual(context.research_packet_sha256, packet.packet_sha256)

    def test_exact_approved_context_and_choice_inserts(self):
        categorizer = FakeCategorizer(select_result(4), authorized=True)

        outcome = self.insert(categorizer=categorizer)

        self.assertEqual(outcome.status, TransactionStatus.INSERTED)
        self.assertEqual(outcome.resolution, Resolution.LLM)
        self.assertEqual(categorizer.authorization_calls[0][2], 4)
        self.assertEqual(self.db.insert_calls[0][1][-1], 4)

    def test_authorized_ignore_choice_is_ignored_not_inserted(self):
        categorizer = FakeCategorizer(select_result(3), authorized=True)

        outcome = self.insert(categorizer=categorizer)

        self.assertEqual(outcome.status, TransactionStatus.IGNORED)
        self.assertEqual(outcome.resolution, Resolution.LLM)
        self.assertEqual(self.db.insert_calls, [])

    def test_abstain_is_unresolved_without_authorization_or_insert(self):
        categorizer = FakeCategorizer(
            EnrichedExecution(decision=EnrichedAbstain("thin", "f" * 64)),
            authorized=True,
        )

        outcome = self.insert(categorizer=categorizer)

        self.assertEqual(outcome.status, TransactionStatus.UNRESOLVED)
        self.assertEqual(outcome.reason, UnresolvedReason.ABSTAINED)
        self.assertEqual(categorizer.authorization_calls, [])
        self.assertEqual(self.db.insert_calls, [])

    def test_suggestion_is_recorded_and_writes_nothing(self):
        decision = EnrichedSuggestion(
            category_name="Gadgets",
            subcategory_name=None,
            parent_category_id=None,
            rationale="widgets",
            evidence_urls=(EVIDENCE_URL,),
            context_fingerprint="f" * 64,
        )
        categorizer = FakeCategorizer(
            EnrichedExecution(decision=decision), authorized=True
        )

        outcome = self.insert(categorizer=categorizer)

        self.assertEqual(outcome.status, TransactionStatus.SUGGESTED)
        (stored,) = self.db.suggestions
        self.assertEqual(outcome.suggestion_id, stored.suggestion_id)
        self.assertIsNone(stored.subcategory_name)
        self.assertEqual(self.db.insert_calls, [])

    def test_provider_failure_keeps_its_reason(self):
        categorizer = FakeCategorizer(
            EnrichedExecution(reason=UnresolvedReason.TIMEOUT)
        )

        self.assertEqual(
            self.insert(categorizer=categorizer).reason, UnresolvedReason.TIMEOUT
        )

    def test_choice_outside_live_categories_is_unresolved(self):
        categorizer = FakeCategorizer(select_result(999), authorized=True)

        outcome = self.insert(categorizer=categorizer)

        self.assertEqual(outcome.status, TransactionStatus.UNRESOLVED)
        self.assertEqual(outcome.reason, UnresolvedReason.INVALID_CHOICE)
        self.assertEqual(self.db.insert_calls, [])

    def test_unusable_packet_reports_its_reason_without_a_provider_call(self):
        for reason in (
            UnresolvedReason.RESEARCH_MISSING,
            UnresolvedReason.RESEARCH_STALE,
            UnresolvedReason.RESEARCH_TAMPERED,
            UnresolvedReason.RESEARCH_UNAPPROVED,
            UnresolvedReason.RESEARCH_IRRELEVANT,
        ):
            for categorizer in (FakeCategorizer(authorized=True), None):
                with self.subTest(reason=reason, categorizer=categorizer):
                    self.db.packet_reason = reason

                    outcome = self.insert(categorizer=categorizer)

                    self.assertEqual(outcome.reason, reason)
                    self.assertEqual(getattr(categorizer, "calls", []), [])
                    self.assertEqual(self.db.insert_calls, [])

    def test_cron_run_never_prompts_and_returns_typed_outcome(self):
        self.db = FakeParentsFinanceDB(cron=True)
        categorizer = FakeCategorizer(select_result(1), authorized=False)

        with patch("builtins.input", side_effect=AssertionError("prompted")):
            outcome = self.insert(categorizer=categorizer)

        self.assertEqual(outcome.status, TransactionStatus.SHADOW)
        self.assertEqual(self.db.insert_calls, [])

    def test_missing_categorizer_returns_provider_error_without_prompt(self):
        with patch("builtins.input", side_effect=AssertionError("prompted")):
            outcome = self.insert()

        self.assertEqual(outcome.status, TransactionStatus.UNRESOLVED)
        self.assertEqual(outcome.reason, UnresolvedReason.PROVIDER_ERROR)
        self.assertEqual(self.db.insert_calls, [])


class TestParentsHeldRows(ParentsFlowTestCase):
    """V2/V3: rules win; unmatched transfers, cheques and cash stay local."""

    def test_unmatched_held_rows_make_no_packet_or_provider_call(self):
        for merchant in ("CAD DRAFT JANE EXAMPLE", "TD ATM W/D 004"):
            with self.subTest(merchant=merchant):
                categorizer = FakeCategorizer(authorized=True)

                outcome = self.insert(categorizer=categorizer, merchant=merchant)

                self.assertEqual(
                    outcome.reason, UnresolvedReason.MANUAL_CONTEXT_REQUIRED
                )
                self.assertEqual(categorizer.calls, [])
                self.assertEqual(self.db.resolved_merchants, [])
                self.assertEqual(self.db.insert_calls, [])

    def test_a_rule_matched_cash_row_still_resolves(self):
        self.db.auto_match = "Utilities"
        categorizer = FakeCategorizer()

        outcome = self.insert(categorizer=categorizer, merchant="TD ATM W/D 004")

        self.assertEqual(outcome.status, TransactionStatus.INSERTED)
        self.assertEqual(outcome.resolution, Resolution.DETERMINISTIC)
        self.assertEqual(categorizer.calls, [])

    def test_markers_inside_an_ordinary_token_are_not_held(self):
        for merchant in ("NEW/DEAL SHOP", "XCHQMART"):
            with self.subTest(merchant=merchant):
                outcome = self.insert(categorizer=FakeCategorizer(), merchant=merchant)

                self.assertEqual(outcome.status, TransactionStatus.SHADOW)


class TestTaxonomySnapshotAndContextReason(ParentsFlowTestCase):
    def test_parents_taxonomy_read_once_across_rows(self):
        reads = []
        original_get_category = self.db.get_category

        def counting_get_category():
            reads.append(1)
            return original_get_category()

        self.db.get_category = counting_get_category
        categorizer = FakeCategorizer(select_result(1), authorized=False)

        for merchant in ("UNKNOWN SHOP", "OTHER SHOP"):
            outcome = self.db.insert_expense(
                self.transaction_date, merchant, 41.5, "", None, categorizer
            )
            self.assertEqual(outcome.status, TransactionStatus.SHADOW)

        self.assertEqual(len(reads), 1)

    def test_non_cent_amount_is_unresolved_invalid_context(self):
        categorizer = FakeCategorizer(select_result(1))

        outcome = self.db.insert_expense(
            self.transaction_date, "HYDRO ONE", 12.345, "", None, categorizer
        )

        self.assertEqual(outcome.status, TransactionStatus.UNRESOLVED)
        self.assertEqual(outcome.reason, UnresolvedReason.INVALID_CONTEXT)
        self.assertEqual(categorizer.contexts, [])
        self.assertEqual(self.db.insert_calls, [])


if __name__ == "__main__":
    unittest.main()

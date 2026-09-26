import unittest
from datetime import date
from unittest.mock import patch

import polars as pl

from db.my_finance import MyFinanceDB
from services.enriched_categorization import PacketResolution
from services.llm_categorizer import EnrichedExecution, EnrichedSelect
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
from sources.ref_data import reimbursement_merchant_ref


EVIDENCE_URL = "https://openai.example/about"


CHOICES = [
    {
        "subcategory_id": 11,
        "category_id": 2,
        "subcategory_name": "Eating Out",
        "category_name": "Food",
    },
    {
        "subcategory_id": 14,
        "category_id": 5,
        "subcategory_name": "Full Reimburse",
        "category_name": "Reimbursement",
    },
    {
        "subcategory_id": 36,
        "category_id": 4,
        "subcategory_name": "AI/Coding",
        "category_name": "Entertainment",
    },
]


def make_packet(normalized_merchant: str = "openai") -> ResearchPacket:
    return ResearchPacket(
        normalized_merchant=normalized_merchant,
        derived_query=normalized_merchant,
        status=PacketStatus.COMPLETE,
        searched_at="2026-09-17T12:00:00+00:00",
        search_results=(
            SearchResult(
                position=0,
                site_name="Example",
                title="Example",
                snippet="An example site",
                url=EVIDENCE_URL,
            ),
        ),
        fetched_pages=(
            FetchedPage(
                url=EVIDENCE_URL,
                final_url=EVIDENCE_URL,
                title="Example",
                description="An example site",
                text="An example page body.",
                relevance_matched_tokens=("example",),
            ),
        ),
    )


def approved_resolver(merchant: str) -> PacketResolution:
    return PacketResolution(packet=make_packet(merchant))


class FakeCategorizer:
    """Stands in for the enriched provider client."""

    def __init__(self, execution):
        self.execution = execution
        self.contexts = []
        self.packets = []

    def categorize_enriched(self, context, packet):
        self.contexts.append(context)
        self.packets.append(packet)
        return self.execution

    def can_write_enriched(self, context, packet, choice_id):
        return False


def select_execution(choice_id):
    return EnrichedExecution(
        decision=EnrichedSelect(
            choice_id=choice_id,
            evidence_urls=(EVIDENCE_URL,),
            context_fingerprint="f" * 64,
        )
    )


class FakeMyFinanceDB(MyFinanceDB):
    reference: tuple[str, str] | None
    reference_error: ValueError | None

    def __init__(self, resolver=approved_resolver):
        self.exact_duplicate = False
        self.reimbursement_duplicate = False
        self.reference = None
        self.reference_error = None
        self.choices = list(CHOICES)
        self.insert_calls = []
        self.auto_match_insert_calls = []
        self._packet_resolver = resolver

    def check_if_expense_exists(self, date, merchant, cost):
        return self.exact_duplicate

    def check_if_reimbursement_expense_exists(self, date, merchant):
        return self.reimbursement_duplicate

    def get_categorization_choices(self):
        return list(self.choices)

    def get_auto_match_category(self, merchant):
        if self.reference_error is not None:
            raise self.reference_error
        return self.reference

    def insert(self, query, args):
        self.insert_calls.append((query, args))

    def insert_into_auto_match(self, merchant, category, subcategory):
        self.auto_match_insert_calls.append((merchant, category, subcategory))


def select_result(choice_id):
    return select_execution(choice_id)


class TaxonomyCountingFinanceDB(MyFinanceDB):
    """Exercises the real cached get_categorization_choices (V43)."""

    def __init__(self, resolver=approved_resolver):
        self.exact_duplicate = False
        self.reimbursement_duplicate = False
        self.reference = None
        self.underlying_reads = 0
        self.insert_calls = []
        self._packet_resolver = resolver

    def check_if_expense_exists(self, date, merchant, cost):
        return self.exact_duplicate

    def check_if_reimbursement_expense_exists(self, date, merchant):
        return self.reimbursement_duplicate

    def get_subcategory_and_category(self):
        self.underlying_reads += 1
        return pl.DataFrame(
            [
                (
                    choice["subcategory_id"],
                    choice["category_id"],
                    choice["subcategory_name"],
                    choice["category_name"],
                )
                for choice in CHOICES
            ],
            schema={
                "subcategory_id": pl.Int64,
                "category_id": pl.Int64,
                "subcategory": pl.Utf8,
                "category": pl.Utf8,
            },
            orient="row",
        )

    def get_auto_match_category(self, merchant):
        return self.reference

    def insert(self, query, args):
        self.insert_calls.append((query, args))


class TestFinanceDeterministicFlow(unittest.TestCase):
    def setUp(self):
        self.db = FakeMyFinanceDB()
        self.transaction_date = date(2026, 9, 15)

    def insert(self, merchant="OPENAI", categorizer=None):
        return self.db.insert_expense(
            self.transaction_date,
            merchant,
            20.0,
            "amex",
            "Services",
            categorizer=categorizer,
        )

    def test_exact_duplicate_returns_before_matching_or_llm(self):
        self.db.exact_duplicate = True
        categorizer = FakeCategorizer(select_result(36))

        outcome = self.insert(categorizer=categorizer)

        self.assertEqual(outcome.status, TransactionStatus.DUPLICATE)
        self.assertEqual(outcome.resolution, Resolution.NONE)
        self.assertEqual(categorizer.contexts, [])
        self.assertEqual(self.db.insert_calls, [])

    def test_known_reimbursement_duplicate_returns_before_llm(self):
        self.db.reimbursement_duplicate = True
        categorizer = FakeCategorizer(select_result(36))

        outcome = self.insert(
            merchant=reimbursement_merchant_ref[0], categorizer=categorizer
        )

        self.assertEqual(outcome.status, TransactionStatus.DUPLICATE)
        self.assertEqual(outcome.resolution, Resolution.DETERMINISTIC)
        self.assertEqual(categorizer.contexts, [])
        self.assertEqual(self.db.insert_calls, [])

    def test_valid_full_pair_inserts_without_llm_or_auto_match_prompt(self):
        self.db.reference = ("Entertainment", "AI/Coding")
        categorizer = FakeCategorizer(select_result(11))

        with patch("builtins.input", side_effect=AssertionError("prompted")):
            outcome = self.insert(categorizer=categorizer)

        self.assertEqual(outcome.status, TransactionStatus.INSERTED)
        self.assertEqual(outcome.resolution, Resolution.DETERMINISTIC)
        self.assertEqual(categorizer.contexts, [])
        self.assertEqual(len(self.db.insert_calls), 1)
        self.assertEqual(self.db.insert_calls[0][1][-2:], (4, 36))
        self.assertEqual(self.db.auto_match_insert_calls, [])

    def test_full_pair_not_subcategory_name_alone_selects_choice(self):
        self.db.choices.insert(
            0,
            {
                "subcategory_id": 99,
                "category_id": 9,
                "subcategory_name": "AI/Coding",
                "category_name": "Work",
            },
        )
        self.db.reference = ("Entertainment", "AI/Coding")

        outcome = self.insert()

        self.assertEqual(outcome.status, TransactionStatus.INSERTED)
        self.assertEqual(self.db.insert_calls[0][1][-2:], (4, 36))

    def test_stale_or_ambiguous_reference_is_unresolved_without_llm(self):
        categorizer = FakeCategorizer(select_result(36))
        references = [
            ("Missing", "Missing"),
            ("Entertainment", "AI/Coding"),
        ]

        for reference in references:
            with self.subTest(reference=reference):
                self.db = FakeMyFinanceDB()
                self.db.reference = reference
                if reference[0] == "Entertainment":
                    self.db.choices.append(dict(CHOICES[-1]))
                outcome = self.insert(categorizer=categorizer)
                self.assertEqual(outcome.status, TransactionStatus.UNRESOLVED)
                self.assertEqual(outcome.resolution, Resolution.DETERMINISTIC)
                self.assertEqual(outcome.reason, UnresolvedReason.INVALID_CHOICE)
                self.assertEqual(self.db.insert_calls, [])

        self.assertEqual(categorizer.contexts, [])

    def test_ambiguous_auto_match_error_is_unresolved_without_llm(self):
        self.db.reference_error = ValueError("multiple matches")
        categorizer = FakeCategorizer(select_result(36))

        outcome = self.insert(categorizer=categorizer)

        self.assertEqual(outcome.status, TransactionStatus.UNRESOLVED)
        self.assertEqual(outcome.reason, UnresolvedReason.INVALID_CHOICE)
        self.assertEqual(categorizer.contexts, [])

    def test_non_cent_amount_is_unresolved_invalid_context(self):
        categorizer = FakeCategorizer(select_result(36))

        outcome = self.db.insert_expense(
            self.transaction_date, "OPENAI", 12.345, "amex", None, categorizer
        )

        self.assertEqual(outcome.status, TransactionStatus.UNRESOLVED)
        self.assertEqual(outcome.reason, UnresolvedReason.INVALID_CONTEXT)
        self.assertEqual(categorizer.contexts, [])
        self.assertEqual(self.db.insert_calls, [])


class TestTaxonomySnapshot(unittest.TestCase):
    def test_finance_taxonomy_read_once_across_rows(self):
        db = TaxonomyCountingFinanceDB()
        categorizer = FakeCategorizer(select_result(36))

        for merchant in ("OPENAI", "ANTHROPIC"):
            outcome = db.insert_expense(
                date(2026, 9, 15), merchant, 10.0, "amex", None, categorizer
            )
            self.assertEqual(outcome.status, TransactionStatus.SHADOW)

        self.assertEqual(db.underlying_reads, 1)


if __name__ == "__main__":
    unittest.main()

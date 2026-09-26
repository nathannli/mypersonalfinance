import unittest
from datetime import date
from unittest.mock import patch

import polars as pl

from db.parents_finance import ParentsFinanceDB
from services.transaction_categorization import (
    CategorizationResult,
    ProviderAction,
    Resolution,
    TransactionStatus,
    UnresolvedReason,
)


CATEGORY_ROWS = [
    (1, "Food"),
    (2, "Utilities"),
    (3, "Ignore"),
    (4, "Transportation"),
]


class FakeCategorizer:
    def __init__(self, result, authorized=False):
        self.result = result
        self.authorized = authorized
        self.contexts = []
        self.authorization_calls = []

    def categorize(self, context):
        self.contexts.append(context)
        return self.result

    def can_write(self, context, choice_id):
        self.authorization_calls.append((context, choice_id))
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

    def check_if_expense_exists(self, date, merchant, cost):
        return self.exact_duplicate

    def get_category(self):
        return pl.DataFrame(
            self.category_rows,
            schema={"id": pl.Int64, "category": pl.Utf8},
            orient="row",
        ).sort("category")

    def get_category_id_from_name(self, category_name):
        if category_name is None:
            return None
        normalized = category_name.replace("\xa0", " ").strip().lower()
        matches = [row[0] for row in self.category_rows if row[1].lower() == normalized]
        if len(matches) > 1:
            raise ValueError(f"Multiple categories found for {category_name}.")
        return matches[0] if matches else None

    def get_auto_match_category(self, merchant):
        if self.auto_match_error is not None:
            raise self.auto_match_error
        return self.auto_match

    def insert(self, query, args):
        self.insert_calls.append((query, args))


def select_result(choice_id):
    return CategorizationResult(ProviderAction.SELECT, choice_id=choice_id)


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
        self.assertEqual(len(categorizer.contexts), 1)
        self.assertEqual(categorizer.contexts[0].database, "parents_finance")
        self.assertEqual(categorizer.contexts[0].amount_minor_units, 4150)

    def test_exact_approved_context_and_choice_inserts(self):
        categorizer = FakeCategorizer(select_result(4), authorized=True)

        outcome = self.insert(categorizer=categorizer)

        self.assertEqual(outcome.status, TransactionStatus.INSERTED)
        self.assertEqual(outcome.resolution, Resolution.LLM)
        self.assertEqual(categorizer.authorization_calls[0][1], 4)
        self.assertEqual(self.db.insert_calls[0][1][-1], 4)

    def test_authorized_ignore_choice_is_ignored_not_inserted(self):
        categorizer = FakeCategorizer(select_result(3), authorized=True)

        outcome = self.insert(categorizer=categorizer)

        self.assertEqual(outcome.status, TransactionStatus.IGNORED)
        self.assertEqual(outcome.resolution, Resolution.LLM)
        self.assertEqual(self.db.insert_calls, [])

    def test_abstain_is_unresolved_without_authorization_or_insert(self):
        categorizer = FakeCategorizer(
            CategorizationResult(
                ProviderAction.ABSTAIN, reason=UnresolvedReason.ABSTAINED
            ),
            authorized=True,
        )

        outcome = self.insert(categorizer=categorizer)

        self.assertEqual(outcome.status, TransactionStatus.UNRESOLVED)
        self.assertEqual(outcome.reason, UnresolvedReason.ABSTAINED)
        self.assertEqual(categorizer.authorization_calls, [])
        self.assertEqual(self.db.insert_calls, [])

    def test_choice_outside_live_categories_is_unresolved(self):
        categorizer = FakeCategorizer(select_result(999), authorized=True)

        outcome = self.insert(categorizer=categorizer)

        self.assertEqual(outcome.status, TransactionStatus.UNRESOLVED)
        self.assertEqual(outcome.reason, UnresolvedReason.INVALID_CHOICE)
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

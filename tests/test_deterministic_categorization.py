"""Tests for the shared pure deterministic resolver (T5).

These pin the behaviour that existed inside ``db/my_finance.py`` before the
extraction, including its quirks, so the refactor is provably behaviour
preserving rather than a quiet rewrite.
"""

from __future__ import annotations

import unittest
from pathlib import Path
from unittest import mock

from services import deterministic_categorization as dc
from services.deterministic_categorization import (
    ROGERS_CARD_TYPE,
    SIMPLII_VISA_CARD_TYPE,
    DeterministicOutcome,
    DeterministicResolution,
    find_exact_auto_match,
    find_reference_choice,
    find_substring_auto_match,
    resolve_deterministic_choice,
    resolve_statement_reference,
)

CHOICES = [
    {
        "subcategory_id": 11,
        "category_id": 1,
        "subcategory_name": "Eating Out",
        "category_name": "Food",
    },
    {
        "subcategory_id": 13,
        "category_id": 1,
        "subcategory_name": "Grocery",
        "category_name": "Food",
    },
    {
        "subcategory_id": 28,
        "category_id": 8,
        "subcategory_name": "Misc",
        "category_name": "Shopping",
    },
    {
        "subcategory_id": 19,
        "category_id": 9,
        "subcategory_name": "Misc",
        "category_name": "Misc",
    },
    {
        "subcategory_id": 30,
        "category_id": 10,
        "subcategory_name": "Travel",
        "category_name": "Travel",
    },
]


class CountingLookup:
    """Stand-in for the database-backed auto-match lookup."""

    def __init__(self, value=None, error: Exception | None = None):
        self.value = value
        self.error = error
        self.calls = 0

    def __call__(self):
        self.calls += 1
        if self.error is not None:
            raise self.error
        return self.value


class TestDeterministicResolution(unittest.TestCase):
    def test_matched_requires_a_choice(self):
        with self.assertRaises(ValueError):
            DeterministicResolution(DeterministicOutcome.MATCHED)

    def test_only_matched_carries_a_choice(self):
        for outcome in (
            DeterministicOutcome.NO_MATCH,
            DeterministicOutcome.INVALID_MAPPING,
        ):
            with self.subTest(outcome=outcome):
                with self.assertRaises(ValueError):
                    DeterministicResolution(outcome, CHOICES[0])

    def test_outcomes_are_exactly_three(self):
        self.assertEqual(
            {member.value for member in DeterministicOutcome},
            {"matched", "no_match", "invalid_mapping"},
        )


class TestFindExactAutoMatch(unittest.TestCase):
    def test_no_rows_is_no_mapping(self):
        self.assertIsNone(find_exact_auto_match("acme", []))

    def test_single_row_returns_the_pair(self):
        self.assertEqual(
            find_exact_auto_match("acme", [("Food", "Grocery")]), ("Food", "Grocery")
        )

    def test_two_rows_for_one_merchant_is_an_error(self):
        # The table is inconsistent, so picking one would mis-categorize.
        with self.assertRaises(ValueError) as caught:
            find_exact_auto_match("acme", [("Food", "Grocery"), ("Travel", "Travel")])
        self.assertIn("Multiple categories found for acme", str(caught.exception))


class TestFindSubstringAutoMatch(unittest.TestCase):
    def test_first_matching_rule_wins(self):
        rows = [("nexus", "Personal Care", "Health"), ("acme", "Food", "Grocery")]
        self.assertEqual(
            find_substring_auto_match("acme store", rows), ("Food", "Grocery")
        )

    def test_several_matches_resolve_to_the_first_row(self):
        rows = [("acme", "Food", "Grocery"), ("store", "Travel", "Travel")]
        self.assertEqual(
            find_substring_auto_match("acme store", rows), ("Food", "Grocery")
        )

    def test_no_match_returns_none(self):
        self.assertIsNone(
            find_substring_auto_match("acme", [("other", "Food", "Grocery")])
        )
        self.assertIsNone(find_substring_auto_match("acme", []))

    def test_stored_pattern_is_compared_as_written(self):
        # Rows are authored lowercase; the merchant alone is lowercased.
        self.assertIsNone(
            find_substring_auto_match("ACME", [("Acme", "Food", "Grocery")])
        )
        self.assertEqual(
            find_substring_auto_match("ACME", [("acme", "Food", "Grocery")]),
            ("Food", "Grocery"),
        )


class TestFindReferenceChoice(unittest.TestCase):
    def test_unique_full_pair_match_is_returned(self):
        self.assertIs(find_reference_choice(CHOICES, ("Food", "Grocery")), CHOICES[1])

    def test_unknown_pair_returns_none(self):
        self.assertIsNone(find_reference_choice(CHOICES, ("Food", "Nonexistent")))

    def test_subcategory_name_alone_is_not_enough(self):
        # 'Misc' exists under both Shopping and Misc, so both names must match.
        self.assertIs(find_reference_choice(CHOICES, ("Shopping", "Misc")), CHOICES[2])
        self.assertIs(find_reference_choice(CHOICES, ("Misc", "Misc")), CHOICES[3])
        self.assertIsNone(find_reference_choice(CHOICES, ("Travel", "Misc")))

    def test_duplicate_live_pairs_are_ambiguous(self):
        duplicated = [CHOICES[1], dict(CHOICES[1])]
        self.assertIsNone(find_reference_choice(duplicated, ("Food", "Grocery")))


class TestResolveStatementReference(unittest.TestCase):
    def test_rogers_uses_the_statement_category_when_available(self):
        lookup = CountingLookup(("Travel", "Travel"))
        reference = resolve_statement_reference(
            ROGERS_CARD_TYPE,
            "Eating Places and Restaurants",
            auto_match=lookup,
        )
        self.assertEqual(reference, ("Food", "Eating Out"))
        self.assertEqual(lookup.calls, 0)

    def test_rogers_without_a_statement_category_falls_back_to_auto_match(self):
        lookup = CountingLookup(("Travel", "Travel"))
        self.assertEqual(
            resolve_statement_reference(ROGERS_CARD_TYPE, None, auto_match=lookup),
            ("Travel", "Travel"),
        )
        self.assertEqual(lookup.calls, 1)

    def test_rogers_unknown_statement_category_falls_back_to_auto_match(self):
        lookup = CountingLookup(("Travel", "Travel"))
        self.assertEqual(
            resolve_statement_reference(
                ROGERS_CARD_TYPE, "Not A Real Rogers Category", auto_match=lookup
            ),
            ("Travel", "Travel"),
        )
        self.assertEqual(lookup.calls, 1)

    def test_simplii_visa_is_always_restaurants_and_never_looks_up(self):
        lookup = CountingLookup(("Travel", "Travel"))
        self.assertEqual(
            resolve_statement_reference(
                SIMPLII_VISA_CARD_TYPE, None, auto_match=lookup
            ),
            ("Food", "Eating Out"),
        )
        self.assertEqual(lookup.calls, 0)

    def test_simplii_visa_ignores_a_statement_category(self):
        lookup = CountingLookup(("Travel", "Travel"))
        self.assertEqual(
            resolve_statement_reference(
                SIMPLII_VISA_CARD_TYPE, "Grocery Stores", auto_match=lookup
            ),
            ("Food", "Eating Out"),
        )
        self.assertEqual(lookup.calls, 0)

    def test_any_other_card_type_uses_auto_match(self):
        lookup = CountingLookup(("Food", "Grocery"))
        self.assertEqual(
            resolve_statement_reference("amex", "Whatever", auto_match=lookup),
            ("Food", "Grocery"),
        )
        self.assertEqual(lookup.calls, 1)


class TestResolveDeterministicChoice(unittest.TestCase):
    def resolve(self, *, card_type="amex", cc_category=None, lookup=None):
        return resolve_deterministic_choice(
            card_type=card_type,
            cc_category=cc_category,
            choices=CHOICES,
            auto_match=lookup if lookup is not None else CountingLookup(None),
        )

    def test_matched_row_carries_the_live_choice(self):
        result = self.resolve(lookup=CountingLookup(("Food", "Grocery")))
        self.assertEqual(result.outcome, DeterministicOutcome.MATCHED)
        self.assertIs(result.choice, CHOICES[1])

    def test_no_mapping_is_a_no_match(self):
        result = self.resolve(lookup=CountingLookup(None))
        self.assertEqual(result.outcome, DeterministicOutcome.NO_MATCH)
        self.assertIsNone(result.choice)

    def test_mapping_absent_from_the_live_taxonomy_is_invalid(self):
        # The mapping exists but the live taxonomy cannot honour it, so the row
        # must not fall through to a packet lookup or an LLM request.
        result = self.resolve(lookup=CountingLookup(("Nonexistent", "Nowhere")))
        self.assertEqual(result.outcome, DeterministicOutcome.INVALID_MAPPING)
        self.assertIsNone(result.choice)

    def test_ambiguous_exact_mapping_is_invalid_not_a_no_match(self):
        with self.assertRaises(ValueError) as caught:
            find_exact_auto_match("acme", [("a", "b"), ("c", "d")])
        ambiguous = CountingLookup(error=caught.exception)
        result = self.resolve(lookup=ambiguous)
        self.assertEqual(result.outcome, DeterministicOutcome.INVALID_MAPPING)
        self.assertEqual(ambiguous.calls, 1)

    def test_statement_mapper_failure_is_invalid_not_a_no_match(self):
        lookup = CountingLookup(("Food", "Grocery"))
        with mock.patch.object(
            dc.RogersStatement,
            "auto_match_category",
            side_effect=ValueError("bad reference table"),
        ):
            result = resolve_deterministic_choice(
                card_type=ROGERS_CARD_TYPE,
                cc_category="Eating Places and Restaurants",
                choices=CHOICES,
                auto_match=lookup,
            )
        self.assertEqual(result.outcome, DeterministicOutcome.INVALID_MAPPING)

    def test_simplii_visa_row_resolves_through_the_statement(self):
        lookup = CountingLookup(None)
        result = self.resolve(card_type=SIMPLII_VISA_CARD_TYPE, lookup=lookup)
        self.assertEqual(result.outcome, DeterministicOutcome.MATCHED)
        self.assertEqual(result.choice["category_name"], "Food")
        self.assertEqual(lookup.calls, 0)

    def test_rogers_row_resolves_from_the_statement_category(self):
        lookup = CountingLookup(None)
        result = self.resolve(
            card_type=ROGERS_CARD_TYPE,
            cc_category="Grocery Stores and Supermarkets",
            lookup=lookup,
        )
        self.assertEqual(result.outcome, DeterministicOutcome.MATCHED)
        self.assertEqual(result.choice["subcategory_name"], "Grocery")
        self.assertEqual(lookup.calls, 0)

    def test_lookup_is_consulted_at_most_once(self):
        lookup = CountingLookup(("Travel", "Travel"))
        self.resolve(lookup=lookup)
        self.assertEqual(lookup.calls, 1)

    def test_resolution_never_raises_for_a_broken_mapping(self):
        cases = (
            CountingLookup(error=ValueError("ambiguous")),
            CountingLookup(("Missing", "Absent")),
            CountingLookup(None),
            CountingLookup(("Food", "Grocery")),
        )
        for lookup in cases:
            with self.subTest(value=lookup.value, error=lookup.error):
                result = self.resolve(lookup=lookup)
                self.assertIsInstance(result.outcome, DeterministicOutcome)


class TestModulePurity(unittest.TestCase):
    """V4: the resolver and its callers can mutate nothing."""

    def setUp(self) -> None:
        self.source = Path(dc.__file__).read_text(encoding="utf-8")

    def test_no_sql_mutation_statements(self):
        lowered = self.source.lower()
        for statement in ("insert ", "update ", "delete ", "commit", "execute("):
            with self.subTest(statement=statement):
                self.assertNotIn(statement, lowered)

    def test_no_database_imports(self):
        self.assertNotIn("import db", self.source)
        self.assertNotIn("db.", self.source)
        self.assertNotIn("psycopg", self.source)

    def test_exposes_no_mutating_attribute(self):
        for name in dir(dc):
            with self.subTest(name=name):
                for verb in ("insert", "delete", "update", "write", "mutate"):
                    self.assertNotIn(verb, name.lower())


if __name__ == "__main__":
    unittest.main()

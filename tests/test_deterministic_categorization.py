"""Tests for the shared pure deterministic resolver (T5).

These pin the behaviour that existed inside ``db/my_finance.py`` before the
extraction, including its quirks, so the refactor is provably behaviour
preserving rather than a quiet rewrite.
"""

from __future__ import annotations

import ast
import random
import re
import unittest
from pathlib import Path
from unittest import mock

from utils.repo_paths import repo_root

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


class TestSubstringOrderInvariance(unittest.TestCase):
    """V10: substring resolution does not depend on the order rules arrive in.

    `find_substring_auto_match` returns the FIRST rule that matches, so the
    answer is only stable when the rules it is given are in a stable order.
    Two things follow, and both are asserted here: the resolver is genuinely
    invariant when the matching rules agree, and the query feeding it is
    pinned to `ORDER BY id` so the answer is the same on every run.
    """

    # Real rules with the same shape as live: several patterns match one
    # merchant, and they all resolve to the same pair.
    AGREEING = [
        ("coffee", "Food", "Eating Out"),
        ("cafe", "Food", "Eating Out"),
        ("espresso", "Food", "Eating Out"),
        ("unrelated", "Travel", "Travel"),
    ]

    def test_agreeing_rules_are_invariant_under_permutation(self):
        for seed in range(25):
            shuffled = list(self.AGREEING)
            random.Random(seed).shuffle(shuffled)
            with self.subTest(seed=seed):
                self.assertEqual(
                    find_substring_auto_match("corner coffee cafe", shuffled),
                    ("Food", "Eating Out"),
                )

    def test_row_order_decides_when_rules_disagree(self):
        # The resolver is first-wins, not consensus. This is the behaviour the
        # ORDER BY exists to pin down, so it is asserted rather than left
        # implicit: a disagreeing pair means the caller's ordering is the
        # whole answer.
        rows = [
            ("corner", "Food", "Eating Out"),
            ("corner", "Travel", "Travel"),
        ]
        self.assertEqual(
            find_substring_auto_match("corner", rows), ("Food", "Eating Out")
        )
        self.assertEqual(
            find_substring_auto_match("corner", list(reversed(rows))),
            ("Travel", "Travel"),
        )


class TestSubstringQueryIsOrdered(unittest.TestCase):
    """The caller pins the order the resolver depends on (T11).

    Reads the query out of the AST rather than by regex, because the literal
    is wrapped across two implicitly concatenated strings. The parser folds
    those back into one constant, so this survives reformatting and line
    wrapping instead of silently matching nothing.
    """

    @classmethod
    def setUpClass(cls) -> None:
        source = (Path(repo_root()) / "db" / "my_finance.py").read_text(
            encoding="utf-8"
        )
        cls.queries = [
            node.value
            for node in ast.walk(ast.parse(source))
            if isinstance(node, ast.Constant)
            and isinstance(node.value, str)
            and node.value.lower().startswith("select")
        ]

    def _query_for(self, table: str) -> str:
        found = [q for q in self.queries if f"from {table}" in q.lower()]
        self.assertEqual(
            len(found), 1, f"expected one query against {table}, found {found}"
        )
        return found[0]

    def test_substring_query_orders_by_id(self):
        # Without this the winning rule is whatever the planner returns.
        # Measured on live: 74 merchants match more than one rule, and forcing
        # enable_seqscan = off already changes the row order.
        query = self._query_for("substring_auto_match")
        self.assertIn(
            "order by id",
            query.lower(),
            f"substring query is unordered: {query!r}",
        )

    def test_exact_query_is_unordered_but_raises_on_conflict(self):
        # Deliberate asymmetry. find_exact_auto_match raises when one merchant
        # has two rows, so its answer never depends on row order and an
        # ORDER BY there would be a no-op dressed as a fix.
        query = self._query_for("merchant_name_auto_match")
        self.assertNotIn("order by", query.lower())


CATEGORIES_INSERT_RE = re.compile(r"VALUES \((\d+), '((?:[^']|'')*)'\)", re.IGNORECASE)
SUBCATEGORIES_INSERT_RE = re.compile(
    r"VALUES \((\d+), '((?:[^']|'')*)', (\d+), (\d+)\)", re.IGNORECASE
)
SEED_ROW_RE = re.compile(
    r"VALUES \((\d+), '((?:[^']|'')*)', '((?:[^']|'')*)', '((?:[^']|'')*)'\)",
    re.IGNORECASE,
)
PARENTS_CATEGORIES_INSERT_RE = re.compile(
    r"VALUES \((\d+), '((?:[^']|'')*)', (\d+)\)", re.IGNORECASE
)
PARENTS_SEED_ROW_RE = re.compile(
    r"VALUES \((\d+), '((?:[^']|'')*)', '((?:[^']|'')*)'\)", re.IGNORECASE
)

# `finance.substring_auto_match` id 78 named ('Shopping', 'Hygiene'), but
# `Hygiene` sits under `Personal Care`. `find_reference_choice` returns None for
# a pair that is not live, so the rule silently never fired. T10 retargeted it
# to ('Personal Care', 'Hygiene'), the pair every other `Hygiene` rule already
# named, so this id is now a repaired row and the tests assert it stays fixed.
# See `test_the_repaired_row_names_the_pair_the_rest_of_the_table_asserts`.
T10_REPAIRED_SUBSTRING_ROW_ID = 78

# T24 repaired this set, so it is now empty and the check below asserts it
# stays empty. It was once five rows naming `Grocery` or `Interest`, neither a
# live `parents_finance` category, plus a `PROMO INTEREST` duplicate that made
# `get_auto_match_category` raise. Recorded here so the regression is
# recognisable rather than merely forbidden.
T24_REPAIRED_PARENT_CATEGORIES = {
    "Grocery": (1,),
    "Interest": (51, 147, 149, 150),
}


def _unquote(value: str) -> str:
    return value.replace("''", "'")


def _insert_lines(path: Path) -> list[str]:
    return [
        line
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip().upper().startswith("INSERT")
    ]


def finance_choices() -> list[dict[str, object]]:
    """The live `finance` choice list, built from the tracked taxonomy DDL."""
    ddl = Path(repo_root()) / "ddl" / "finance"
    categories = {
        int(cid): _unquote(name)
        for cid, name in CATEGORIES_INSERT_RE.findall(
            "\n".join(_insert_lines(ddl / "categories.sql"))
        )
    }
    return [
        {
            "subcategory_id": int(sid),
            "category_id": int(cid),
            "subcategory_name": _unquote(name),
            "category_name": categories[int(cid)],
        }
        for sid, name, cid, _ in SUBCATEGORIES_INSERT_RE.findall(
            "\n".join(_insert_lines(ddl / "subcategories.sql"))
        )
    ]


def parents_choices() -> dict[str, str]:
    """`parents_finance` category id -> name, from the tracked DDL."""
    path = Path(repo_root()) / "ddl" / "parents_finance" / "categories.sql"
    return {
        int(cid): _unquote(name)
        for cid, name, _ in PARENTS_CATEGORIES_INSERT_RE.findall(
            "\n".join(_insert_lines(path))
        )
    }


@unittest.skipUnless(
    (Path(repo_root()) / "ddl" / "seed").is_dir(),
    "ddl/seed/ is absent (a fresh clone or CI); the live-pair check needs the "
    "generated seed",
)
class TestAutoMatchRowsNameLivePairs(unittest.TestCase):
    """V9: every auto-match row names a pair the taxonomy actually has.

    `find_reference_choice` returns `None` for a pair that is not live, and the
    caller treats `None` as "no mapping" rather than as an error. So a bad pair
    does not fail loudly; the rule just silently never fires. This asserts the
    pair set instead, which is the only place the problem is visible.

    Skipped without `ddl/seed/`, so CI still runs it as a skip.
    """

    @classmethod
    def setUpClass(cls) -> None:
        cls.seed = Path(repo_root()) / "ddl" / "seed"
        cls.choices = finance_choices()

    def test_the_fixture_parsed_a_real_taxonomy(self) -> None:
        # Guard the vacuous pass. If the regexes stopped matching, the
        # assertions below would pass over an empty list and prove nothing.
        self.assertEqual(len(self.choices), 36, "expected 36 live subcategories")
        names = {(c["category_name"], c["subcategory_name"]) for c in self.choices}
        self.assertIn(("Misc", "Misc"), names)
        self.assertIn(("Shopping", "Misc"), names)

    def _unresolved_finance_rows(self) -> list[tuple]:
        """Seed rows naming a pair the live taxonomy does not have."""
        unresolved: list[tuple] = []
        for table in (
            "finance.merchant_name_auto_match",
            "finance.substring_auto_match",
        ):
            path = self.seed / f"{table}.sql"
            self.assertTrue(path.is_file(), f"missing {path.name}")
            rows = SEED_ROW_RE.findall(path.read_text(encoding="utf-8"))
            self.assertTrue(rows, f"{path.name}: parsed no rows")
            for row_id, merchant, category, subcategory in rows:
                if (
                    find_reference_choice(
                        self.choices, (_unquote(category), _unquote(subcategory))
                    )
                    is None
                ):
                    unresolved.append(
                        (
                            table,
                            int(row_id),
                            _unquote(merchant),
                            _unquote(category),
                            _unquote(subcategory),
                        )
                    )
        return unresolved

    def test_finance_auto_match_rows_name_live_pairs(self) -> None:
        unresolved = self._unresolved_finance_rows()
        self.assertEqual(
            [],
            unresolved,
            f"{len(unresolved)} row(s) name a (category, subcategory) pair that is "
            f"not in the live taxonomy, so the rule can never resolve: "
            f"{unresolved[:5]}",
        )

    def test_the_repaired_row_names_the_pair_the_rest_of_the_table_asserts(
        self,
    ) -> None:
        """T10's target was not a guess, so pin why it is right.

        Id 78 now names `('Personal Care', 'Hygiene')`. Every other `Hygiene`
        rule in the table already named that pair, and `paula's choice` is a
        cosmetics brand among 12 other `Personal Care` merchants. If a future
        taxonomy move renames or reparents `Hygiene`, this fails instead of the
        repair silently becoming another dead rule.
        """
        rows = SEED_ROW_RE.findall(
            (self.seed / "finance.substring_auto_match.sql").read_text(encoding="utf-8")
        )
        by_id = {int(row[0]): row for row in rows}
        self.assertIn(T10_REPAIRED_SUBSTRING_ROW_ID, by_id)
        row = by_id[T10_REPAIRED_SUBSTRING_ROW_ID]
        self.assertEqual(
            ("paula's choice", "Personal Care", "Hygiene"),
            (_unquote(row[1]), _unquote(row[2]), _unquote(row[3])),
        )

        pairs = [
            (_unquote(r[2]), _unquote(r[3]))
            for r in rows
            if _unquote(r[3]) == "Hygiene"
            and int(r[0]) != T10_REPAIRED_SUBSTRING_ROW_ID
        ]
        self.assertTrue(pairs, "expected other Hygiene rules in the table")
        self.assertEqual(
            {("Personal Care", "Hygiene")},
            set(pairs),
            "the other Hygiene rules no longer agree on the pair, so the "
            "repaired row's category is no longer supported by the table",
        )

    def test_the_known_bad_pair_is_repaired(self) -> None:
        """T10 repaired id 78, so the whole set must now be empty.

        It was the one rule naming `('Shopping', 'Hygiene')` while `Hygiene`
        sits under `Personal Care`. Such a rule can never resolve and nothing
        reported it, which is the whole reason V9 exists. T10 retargeted it to
        `('Personal Care', 'Hygiene')` -- the pair all 4 other `Hygiene` rows
        already named -- so this now runs as a real assertion and stays one.
        """
        self.assertEqual([], self._unresolved_finance_rows())

    def test_parents_auto_match_rows_name_live_categories(self) -> None:
        # parents_finance is one level: a rule names a category, not a pair.
        by_id = parents_choices()
        self.assertEqual(len(by_id), 22, "expected 22 live parents categories")
        live = set(by_id.values())
        rows = PARENTS_SEED_ROW_RE.findall(
            (self.seed / "parents_finance.auto_match.sql").read_text(encoding="utf-8")
        )
        self.assertEqual(len(rows), 203, "expected 203 live parents auto_match rows")
        self.assertEqual(
            {},
            {
                category: tuple(
                    int(row_id)
                    for row_id, _, name in rows
                    if _unquote(name) == category
                )
                for category in sorted({_unquote(name) for _, _, name in rows} - live)
            },
            "a parents rule is naming a non-live category again; T24 removed "
            f"exactly these, so a reappearance is a regression: {T24_REPAIRED_PARENT_CATEGORIES}",
        )

        # The duplicate T24 removed. Two exact rows for one merchant is what
        # made get_auto_match_category raise, and the 2-column UNIQUE permits
        # it, so nothing else would have caught it.
        seen: dict[str, list[int]] = {}
        for row_id, merchant, _ in rows:
            seen.setdefault(_unquote(merchant), []).append(int(row_id))
        self.assertEqual(
            {},
            {m: ids for m, ids in seen.items() if len(ids) > 1},
            "a merchant has two exact auto_match rows again; that raises at "
            "runtime and the 2-column UNIQUE does not prevent it",
        )

        substring_rows = PARENTS_SEED_ROW_RE.findall(
            (self.seed / "parents_finance.substring_auto_match.sql").read_text(
                encoding="utf-8"
            )
        )
        self.assertTrue(substring_rows, "parsed no parents substring rows")
        unknown = sorted({_unquote(name) for _, _, name in substring_rows} - live)
        self.assertEqual(
            [],
            unknown,
            f"parents substring rules naming a non-live category: {unknown}",
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

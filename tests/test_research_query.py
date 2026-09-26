"""Tests for deterministic query derivation and the relevance prefilter (T2)."""

from __future__ import annotations

import unittest

from services import research_query
from services.research_query import (
    MIN_SIGNIFICANT_TOKEN_LENGTH,
    PROCESSOR_PREFIXES,
    STOPWORDS,
    derive_query,
    matched_relevance_tokens,
    normalize_for_matching,
    query_tokens,
    significant_tokens,
)
from services.tinyfish_research import ResearchIrrelevantError


class TestQueryDerivation(unittest.TestCase):
    def test_strips_every_known_processor_prefix_case_insensitively(self):
        for prefix in PROCESSOR_PREFIXES:
            with self.subTest(prefix=prefix):
                descriptor = f"{prefix.upper()}ACME WIDGETS"
                self.assertEqual(derive_query(descriptor), "acme widgets")

    def test_strips_repeated_processor_prefixes(self):
        self.assertEqual(derive_query("PAYPAL *SQ * ACME"), "acme")
        self.assertEqual(derive_query("TST-STRIPE-ACME INC"), "acme inc")

    def test_strips_trailing_store_order_and_phone_digit_runs(self):
        self.assertEqual(derive_query("ACME 4471"), "acme")
        self.assertEqual(derive_query("ACME 647 555 1234"), "acme")
        self.assertEqual(derive_query("ACME #4471"), "acme")

    def test_strips_trailing_city_province_and_country_fragments(self):
        self.assertEqual(derive_query("ACME WIDGETS TORONTO"), "acme widgets")
        self.assertEqual(derive_query("ACME WIDGETS TORONT ON"), "acme widgets")
        self.assertEqual(derive_query("ACME WIDGETS MISSISSAUGA"), "acme widgets")
        self.assertEqual(derive_query("LAST Z SURVIVOR SG"), "last z survivor")

    def test_strips_trailing_country_code_tld(self):
        self.assertEqual(derive_query("STAPLES.CA/48620392128  MISSISSAUGA"), "staples")

    def test_strips_interleaved_digit_runs_and_location_fragments(self):
        self.assertEqual(derive_query("ACME 4471 TORONTO 88 ON"), "acme")

    def test_only_trailing_location_fragments_are_stripped(self):
        self.assertEqual(derive_query("TORONTO ACME"), "toronto acme")

    def test_collapses_separators_and_punctuation(self):
        self.assertEqual(derive_query("ACME--WIDGETS, INC."), "acme widgets inc")
        self.assertEqual(derive_query("ACME   ###   WIDGETS"), "acme widgets")

    def test_normalizes_case_fullwidth_and_nbsp(self):
        self.assertEqual(derive_query("  ＡＣＭＥ\xa0  WIDGETS  "), "acme widgets")
        self.assertEqual(derive_query("AcMe WiDgEtS"), "acme widgets")

    def test_real_statement_descriptors_derive_a_searchable_term(self):
        self.assertEqual(
            derive_query("CONG CAPHE EXAMPLE ### TORONT"), "cong caphe example"
        )
        self.assertEqual(derive_query("AIRWALXSG*LAST Z SURVIVOR SG"), "last z survivor")
        self.assertEqual(derive_query("PAYPAL *AICAMERCHANT 6475551234"), "aicamerchant")

    def test_never_reduces_the_descriptor_to_nothing(self):
        # A lone location token stands rather than being stripped away entirely.
        self.assertEqual(derive_query("TORONTO"), "toronto")

    def test_empty_or_digits_only_terms_are_typed_failures(self):
        for descriptor in ("", "   ", "###", "4471", "1 2 3 4471"):
            with self.subTest(descriptor=descriptor):
                with self.assertRaises(ResearchIrrelevantError):
                    derive_query(descriptor)

    def test_non_string_descriptor_is_a_typed_failure(self):
        with self.assertRaises(ResearchIrrelevantError):
            derive_query(None)  # type: ignore[arg-type]


class TestSignificantTokens(unittest.TestCase):
    def test_applies_length_and_stopword_rules_and_sorts(self):
        self.assertEqual(
            significant_tokens("widgets acme inc the of"), ("acme", "widgets")
        )
        self.assertEqual(significant_tokens(f"{'x' * 4} abc"), ("xxxx",))
        self.assertEqual(MIN_SIGNIFICANT_TOKEN_LENGTH, 4)

    def test_deduplicates_repeated_tokens(self):
        self.assertEqual(significant_tokens("acme acme widgets"), ("acme", "widgets"))

    def test_returns_nothing_for_short_or_stopword_only_terms(self):
        # V53: zero significant tokens is a typed failure raised before Search.
        for term in ("a b c", "the and for with", "", "abc"):
            with self.subTest(term=term):
                self.assertEqual(significant_tokens(term), ())

    def test_stopword_set_is_frozen_and_contains_the_descriptor_noise(self):
        self.assertIsInstance(STOPWORDS, frozenset)
        for word in ("inc", "ltd", "the", "store", "online"):
            self.assertIn(word, STOPWORDS)


class TestRelevancePrefilter(unittest.TestCase):
    def test_matches_whole_tokens_only(self):
        self.assertEqual(
            matched_relevance_tokens(("acme",), ("Acme Corp makes widgets",)),
            ("acme",),
        )
        self.assertEqual(
            matched_relevance_tokens(("acme",), ("Acmecorp makes widgets",)), ()
        )

    def test_matching_normalizes_case_and_fullwidth_identically(self):
        self.assertEqual(
            matched_relevance_tokens(("acme", "widgets"), ("ＡＣＭＥ  WIDGETS",)),
            ("acme", "widgets"),
        )
        self.assertEqual(
            normalize_for_matching("ＡＣＭＥ"), normalize_for_matching("acme")
        )

    def test_matching_unions_every_supplied_text(self):
        matched = matched_relevance_tokens(
            ("acme", "sturgeon"),
            ("Sturgeon page title", "description mentioning acme", ""),
        )
        self.assertEqual(matched, ("acme", "sturgeon"))

    def test_returns_nothing_when_no_text_matches(self):
        self.assertEqual(matched_relevance_tokens(("acme",), ("unrelated page",)), ())
        self.assertEqual(matched_relevance_tokens(("acme",), ("",)), ())

    def test_preserves_token_order_and_only_ever_returns_supplied_tokens(self):
        self.assertEqual(
            matched_relevance_tokens(("alpha", "beta"), ("beta alpha",)),
            ("alpha", "beta"),
        )

    def test_token_match_is_a_prefilter_and_grants_no_approval(self):
        # V45: matching can only retain or drop evidence; eligibility comes only
        # from an explicit human approval record stored elsewhere.
        tokens = ("acme",)
        matched = matched_relevance_tokens(tokens, ("acme acme acme",))
        self.assertTrue(set(matched).issubset(set(tokens)))
        for name in dir(research_query):
            self.assertNotIn("approv", name.lower())
            self.assertNotIn("eligib", name.lower())

    def test_query_tokens_share_the_same_normalization(self):
        self.assertEqual(query_tokens("ＡＣＭＥ\xa0 Widgets"), ("acme", "widgets"))


if __name__ == "__main__":
    unittest.main()

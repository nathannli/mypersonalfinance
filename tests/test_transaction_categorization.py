import unittest
from decimal import Decimal

from services.transaction_categorization import (
    ProviderAction,
    Resolution,
    TransactionOutcome,
    TransactionStatus,
    UnresolvedReason,
    amount_to_minor_units,
    build_canonical_context,
    canonicalize_choices,
    normalize_context_text,
    normalize_optional_context_text,
)


FINANCE_CHOICES = [
    {
        "subcategory_id": 36,
        "category_id": 4,
        "subcategory_name": "AI/Coding",
        "category_name": "Entertainment",
    },
    {
        "subcategory_id": 11,
        "category_id": 2,
        "subcategory_name": "Eating Out",
        "category_name": "Food",
    },
]

PARENTS_CHOICES = [
    {"category_id": 9, "category_name": "Food"},
    {"category_id": 3, "category_name": "Entertainment"},
]


class TestTransactionCategorization(unittest.TestCase):
    def test_outcome_contract_exposes_all_statuses_and_reasons(self):
        self.assertEqual(
            {status.value for status in TransactionStatus},
            {
                "inserted",
                "duplicate",
                "ignored",
                "deleted",
                "unresolved",
                "shadow",
                "suggested",
            },
        )
        self.assertEqual(
            {reason.value for reason in UnresolvedReason},
            {
                "abstained",
                "timeout",
                "provider_error",
                "malformed",
                "invalid_choice",
                "invalid_context",
                "circuit_open",
                "research_auth",
                "research_rate_limit",
                "research_timeout",
                "research_provider_error",
                "research_no_results",
                "research_no_valid_urls",
                "research_fetch_failed",
                "research_empty_evidence",
                "research_irrelevant",
                "research_malformed",
                "research_missing",
                "research_stale",
                "research_tampered",
                "research_unapproved",
            },
        )
        self.assertEqual(
            {action.value for action in ProviderAction},
            {"select", "suggest_new", "abstain", "unresolved"},
        )
        self.assertEqual(
            TransactionOutcome(TransactionStatus.INSERTED, Resolution.DETERMINISTIC),
            TransactionOutcome(TransactionStatus.INSERTED, Resolution.DETERMINISTIC),
        )

    def test_normalization_uses_nfkc_nbsp_whitespace_trim_and_casefold(self):
        self.assertEqual(
            normalize_context_text("  ＯＰＥＮＡＩ\xa0  INC.  "), "openai inc."
        )
        self.assertIsNone(normalize_optional_context_text("  \xa0 "))
        self.assertIsNone(normalize_optional_context_text(None))

    def test_amount_conversion_requires_exact_finite_cents(self):
        self.assertEqual(amount_to_minor_units(Decimal("12.34")), 1234)
        self.assertEqual(amount_to_minor_units("-0.01"), -1)
        self.assertEqual(amount_to_minor_units(2), 200)

        for invalid in ("1.001", "NaN", "Infinity", "not-money"):
            with self.subTest(invalid=invalid):
                with self.assertRaises(ValueError):
                    amount_to_minor_units(invalid)

    def test_finance_choices_are_canonical_and_sorted_by_subcategory_id(self):
        choices = canonicalize_choices("finance", reversed(FINANCE_CHOICES))

        self.assertEqual([choice["subcategory_id"] for choice in choices], [11, 36])
        self.assertEqual(
            choices[0],
            {
                "category_id": 2,
                "category_name": "Food",
                "subcategory_id": 11,
                "subcategory_name": "Eating Out",
            },
        )

    def test_parents_choices_are_canonical_and_sorted_by_category_id(self):
        choices = canonicalize_choices("parents_finance", PARENTS_CHOICES)

        self.assertEqual([choice["category_id"] for choice in choices], [3, 9])

    def test_choice_ids_reject_bool_duplicates_and_wrong_database_shape(self):
        with self.assertRaisesRegex(ValueError, "integer"):
            canonicalize_choices(
                "parents_finance",
                [{"category_id": True, "category_name": "Food"}],
            )
        with self.assertRaisesRegex(ValueError, "unique"):
            canonicalize_choices(
                "parents_finance",
                [
                    {"category_id": 1, "category_name": "Food"},
                    {"category_id": 1, "category_name": "Travel"},
                ],
            )
        with self.assertRaisesRegex(ValueError, "invalid fields"):
            canonicalize_choices(
                "finance", [{"category_id": 1, "category_name": "Food"}]
            )

    def test_context_bytes_and_fingerprint_are_stable_across_choice_order(self):
        first = build_canonical_context(
            database="finance",
            merchant="  OPENAI\xa0 ",
            amount="12.30",
            statement_category=" Other  Services ",
            allowed_choices=FINANCE_CHOICES,
        )
        second = build_canonical_context(
            database="finance",
            merchant="openai",
            amount=Decimal("12.3"),
            statement_category="other services",
            allowed_choices=reversed(FINANCE_CHOICES),
        )

        self.assertEqual(first, second)
        self.assertEqual(first.fingerprint, second.fingerprint)
        self.assertEqual(first.to_bytes(), second.to_bytes())
        self.assertEqual(first.as_dict()["amount_minor_units"], 1230)

    def test_context_fingerprint_changes_with_every_semantic_input(self):
        base = {
            "database": "finance",
            "merchant": "OPENAI",
            "amount": "12.30",
            "statement_category": "services",
            "allowed_choices": FINANCE_CHOICES,
        }
        original = build_canonical_context(**base).fingerprint

        variants = [
            {
                **base,
                "database": "parents_finance",
                "allowed_choices": PARENTS_CHOICES,
            },
            {**base, "merchant": "GITHUB"},
            {**base, "amount": "12.31"},
            {**base, "statement_category": "software"},
            {
                **base,
                "allowed_choices": [
                    *FINANCE_CHOICES,
                    {
                        "subcategory_id": 30,
                        "category_id": 8,
                        "subcategory_name": "Travel",
                        "category_name": "Travel",
                    },
                ],
            },
        ]

        self.assertTrue(
            all(
                build_canonical_context(**variant).fingerprint != original
                for variant in variants
            )
        )

    def test_context_rejects_blank_merchant_and_empty_choices(self):
        with self.assertRaisesRegex(ValueError, "merchant"):
            build_canonical_context(
                database="finance",
                merchant=" \xa0 ",
                amount="1.00",
                statement_category=None,
                allowed_choices=FINANCE_CHOICES,
            )
        with self.assertRaisesRegex(ValueError, "must not be empty"):
            build_canonical_context(
                database="finance",
                merchant="OPENAI",
                amount="1.00",
                statement_category=None,
                allowed_choices=[],
            )


if __name__ == "__main__":
    unittest.main()

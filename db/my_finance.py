from datetime import date

import polars as pl

from sources.ref_data import reimbursement_merchant_ref
from sources.csv.rogers import RogersStatement
from sources.csv.simplii_visa import SimpliiVisaStatement
from db.finance_base import FinanceDB
from services.llm_categorizer import OpenCodexCategorizer
from services.transaction_categorization import (
    ProviderAction,
    Resolution,
    TransactionOutcome,
    TransactionStatus,
    UnresolvedReason,
    build_canonical_context,
)


class MyFinanceDB(FinanceDB):
    reimbursement_subcategory_id = 14

    # Taxonomy snapshot: read once per run (V43). Class-level None default
    # so subclasses that skip __init__ still observe the lazy cache.
    _categorization_choices: list[dict[str, object]] | None = None

    def __init__(self, debug: bool = False):
        super().__init__(database_name="finance", debug=debug)

    def get_subcategory_and_category(self) -> pl.DataFrame:
        """
        Get all subcategories and categories.
        """
        query = """
        select 
            subcategories.id as subcategory_id, 
            categories.id as category_id,
            subcategories.name as subcategory, 
            categories.name as category 
        from subcategories 
            join categories on 
                subcategories.category_id = categories.id
        """
        schema = {
            "subcategory_id": pl.Int64,
            "category_id": pl.Int64,
            "subcategory": pl.Utf8,
            "category": pl.Utf8,
        }
        return pl.DataFrame(self.select(query), schema=schema, orient="row").sort(
            by=["category", "subcategory"]
        )

    def check_if_reimbursement_expense_exists(self, date: date, merchant: str) -> bool:
        """
        Check if a reimbursement expense exists in the database.
        """
        return self._check_exists("expenses", {"date": date, "merchant": merchant})

    def get_categorization_choices(self) -> list[dict[str, object]]:
        if self._categorization_choices is None:
            self._categorization_choices = [
                {
                    "subcategory_id": row["subcategory_id"],
                    "category_id": row["category_id"],
                    "subcategory_name": row["subcategory"],
                    "category_name": row["category"],
                }
                for row in self.get_subcategory_and_category().iter_rows(named=True)
            ]
        return self._categorization_choices

    @staticmethod
    def _is_reimbursement_merchant(merchant: str) -> bool:
        return any(
            merchant.lower() in reimbursement_merchant.lower()
            for reimbursement_merchant in reimbursement_merchant_ref
        )

    def _get_reference_category(
        self, merchant: str, card_type: str, cc_category: str | None
    ) -> tuple[str, str] | None:
        if card_type == "rogers" and cc_category is not None:
            reference = RogersStatement.auto_match_category(cc_category)
            return reference or self.get_auto_match_category(merchant)
        if card_type == "simplii_visa":
            return SimpliiVisaStatement.auto_match_category()
        return self.get_auto_match_category(merchant)

    @staticmethod
    def _find_reference_choice(
        choices: list[dict[str, object]], reference: tuple[str, str]
    ) -> dict[str, object] | None:
        category, subcategory = reference
        matches = [
            choice
            for choice in choices
            if choice["category_name"] == category
            and choice["subcategory_name"] == subcategory
        ]
        if len(matches) != 1:
            return None
        return matches[0]

    def _insert_choice(
        self,
        date: date,
        merchant: str,
        cost: float,
        choice: dict[str, object],
        resolution: Resolution,
    ) -> TransactionOutcome:
        category_id = choice["category_id"]
        subcategory_id = choice["subcategory_id"]
        if (
            isinstance(category_id, bool)
            or not isinstance(category_id, int)
            or isinstance(subcategory_id, bool)
            or not isinstance(subcategory_id, int)
        ):
            return TransactionOutcome(
                TransactionStatus.UNRESOLVED,
                resolution,
                UnresolvedReason.INVALID_CHOICE,
            )
        if (
            self._is_reimbursement_merchant(merchant)
            or subcategory_id == self.reimbursement_subcategory_id
        ) and self.check_if_reimbursement_expense_exists(date, merchant):
            return TransactionOutcome(TransactionStatus.DUPLICATE, resolution)
        query = "insert into expenses (date, merchant, cost, category_id, subcategory_id) values (%s, %s, %s, %s, %s)"
        self.insert(query, (date, merchant, cost, category_id, subcategory_id))
        return TransactionOutcome(TransactionStatus.INSERTED, resolution)

    def insert_expense(
        self,
        date: date,
        merchant: str,
        cost: float,
        card_type: str,
        cc_category: str | None = None,
        categorizer: OpenCodexCategorizer | None = None,
    ) -> TransactionOutcome:
        print(f"Transaction on {date} at {merchant} for {cost}")
        if self.check_if_expense_exists(date, merchant, cost):
            return TransactionOutcome(TransactionStatus.DUPLICATE)
        if self._is_reimbursement_merchant(
            merchant
        ) and self.check_if_reimbursement_expense_exists(date, merchant):
            return TransactionOutcome(
                TransactionStatus.DUPLICATE, Resolution.DETERMINISTIC
            )

        choices = self.get_categorization_choices()
        try:
            reference = self._get_reference_category(merchant, card_type, cc_category)
        except ValueError:
            return TransactionOutcome(
                TransactionStatus.UNRESOLVED,
                Resolution.DETERMINISTIC,
                UnresolvedReason.INVALID_CHOICE,
            )

        if reference is not None:
            choice = self._find_reference_choice(choices, reference)
            if choice is None:
                return TransactionOutcome(
                    TransactionStatus.UNRESOLVED,
                    Resolution.DETERMINISTIC,
                    UnresolvedReason.INVALID_CHOICE,
                )
            return self._insert_choice(
                date, merchant, cost, choice, Resolution.DETERMINISTIC
            )

        if categorizer is None:
            return TransactionOutcome(
                TransactionStatus.UNRESOLVED,
                reason=UnresolvedReason.PROVIDER_ERROR,
            )

        try:
            context = build_canonical_context(
                database="finance",
                merchant=merchant,
                amount=cost,
                statement_category=cc_category,
                allowed_choices=choices,
            )
        except ValueError:
            return TransactionOutcome(
                TransactionStatus.UNRESOLVED,
                reason=UnresolvedReason.INVALID_CONTEXT,
            )

        result = categorizer.categorize(context)
        if result.action != ProviderAction.SELECT or result.choice_id is None:
            return TransactionOutcome(
                TransactionStatus.UNRESOLVED,
                reason=result.reason or UnresolvedReason.MALFORMED,
            )

        choice = next(
            (item for item in choices if item["subcategory_id"] == result.choice_id),
            None,
        )
        if choice is None:
            return TransactionOutcome(
                TransactionStatus.UNRESOLVED,
                reason=UnresolvedReason.INVALID_CHOICE,
            )
        if not categorizer.can_write(context, result.choice_id):
            return TransactionOutcome(
                TransactionStatus.SHADOW,
                Resolution.LLM,
                suggested_choice_id=result.choice_id,
            )
        return self._insert_choice(date, merchant, cost, choice, Resolution.LLM)

    def get_auto_match_category(self, merchant: str) -> tuple[str, str] | None:
        """
        Get the category and subcategory for the merchant.
        """
        query = "select merchant_category, merchant_subcategory from merchant_name_auto_match where merchant_name = %s"
        result = self.select(query, (merchant,))
        if len(result) > 1:
            raise ValueError(
                f"Multiple categories found for {merchant}. Something is wrong."
            )
        elif len(result) == 1:
            return result[0]
        else:
            # try substring auto match
            query = "select substring, merchant_category, merchant_subcategory from substring_auto_match"
            result = self.select(query)
            substring_matches = list()
            for item in result:
                if item[0] in merchant.lower():
                    substring_matches.append((item[1], item[2]))
            if len(substring_matches) >= 1:
                return substring_matches[0]
            else:
                return None

    def insert_into_auto_match(
        self, merchant: str, category: str, subcategory: str
    ) -> None:
        """
        Insert a new merchant into the auto_match table.
        """
        query = "insert into merchant_name_auto_match (merchant_name, merchant_category, merchant_subcategory) values (%s, %s, %s)"
        self.insert(query, (merchant, category, subcategory))

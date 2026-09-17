from datetime import date

import polars as pl

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


class ParentsFinanceDB(FinanceDB):
    cron: bool

    # Taxonomy snapshot: read once per run (V43). Class-level None default
    # so subclasses that skip __init__ still observe the lazy cache.
    _categorization_choices: list[dict[str, object]] | None = None

    def __init__(self, debug: bool = False, cron: bool = False):
        super().__init__(database_name="parents_finance", debug=debug)
        self.cron = cron

    def get_category(self) -> pl.DataFrame:
        """
        Get all categories.
        """
        query = "select id, name as category from categories"
        return pl.DataFrame(
            self.select(query),
            schema={"id": pl.Int64, "category": pl.Utf8},
            orient="row",
        ).sort("category")

    def get_category_name_from_id(self, category_id: int) -> str:
        """
        Get the category name from the category id.
        """
        query = "select name as category from categories where id = %s"
        return self.select(query, (category_id,))[0][0]

    def get_category_id_from_name(self, category_name: str | None) -> int | None:
        """
        Get the category id from the category name.
        """
        if category_name is None:
            return None

        # Normalize whitespace from CSV exports (notably non-breaking spaces).
        category_name = category_name.replace("\xa0", " ").strip()

        query = "select id from categories where lower(name) = lower(%s)"
        result = self.select(query, (category_name,))
        if len(result) == 1:
            return result[0][0]
        elif len(result) > 1:
            print(f"Multiple categories found for {category_name}. Something is wrong.")
            raise ValueError(
                f"Multiple categories found for {category_name}. Something is wrong."
            )
        else:
            return None

    def get_categorization_choices(self) -> list[dict[str, object]]:
        if self._categorization_choices is None:
            self._categorization_choices = [
                {
                    "category_id": row["id"],
                    "category_name": row["category"],
                }
                for row in self.get_category().iter_rows(named=True)
            ]
        return self._categorization_choices

    @staticmethod
    def _find_choice(
        choices: list[dict[str, object]], category_id: int
    ) -> dict[str, object] | None:
        matches = [choice for choice in choices if choice["category_id"] == category_id]
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
        category_name = choice["category_name"]
        if (
            isinstance(category_id, bool)
            or not isinstance(category_id, int)
            or not isinstance(category_name, str)
        ):
            return TransactionOutcome(
                TransactionStatus.UNRESOLVED,
                resolution,
                UnresolvedReason.INVALID_CHOICE,
            )
        if category_name.strip().lower() == "ignore":
            return TransactionOutcome(TransactionStatus.IGNORED, resolution)
        query = "insert into expenses (date, merchant, cost, category_id) values (%s, %s, %s, %s)"
        self.insert(query, (date, merchant, cost, category_id))
        return TransactionOutcome(TransactionStatus.INSERTED, resolution)

    def insert_expense(
        self,
        date: date,
        merchant: str,
        cost: float,
        card_type: str = "",
        cc_category: str | None = None,
        categorizer: OpenCodexCategorizer | None = None,
    ) -> TransactionOutcome:
        print(f"Transaction on {date} at {merchant} for {cost}")
        if self.check_if_expense_exists(date, merchant, cost):
            return TransactionOutcome(TransactionStatus.DUPLICATE)

        choices = self.get_categorization_choices()
        try:
            category_id = self.get_category_id_from_name(cc_category)
            if category_id is None:
                category = self.get_auto_match_category(merchant)
                if category is not None:
                    category_id = self.get_category_id_from_name(category)
                    if category_id is None:
                        # V44: a stale mapping is surfaced deterministically,
                        # never silently rerouted to the LLM path.
                        return TransactionOutcome(
                            TransactionStatus.UNRESOLVED,
                            Resolution.DETERMINISTIC,
                            UnresolvedReason.INVALID_CHOICE,
                        )
        except ValueError:
            return TransactionOutcome(
                TransactionStatus.UNRESOLVED,
                Resolution.DETERMINISTIC,
                UnresolvedReason.INVALID_CHOICE,
            )

        if category_id is not None:
            choice = self._find_choice(choices, category_id)
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
                database="parents_finance",
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

        choice = self._find_choice(choices, result.choice_id)
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

    def get_auto_match_category(self, merchant: str) -> str | None:
        """
        Get the category for the merchant.
        """
        query = "select merchant_category from auto_match where merchant_name = %s"
        result = self.select(query, (merchant,))
        if len(result) > 1:
            raise ValueError(
                f"Multiple categories found for {merchant}. Something is wrong."
            )
        elif len(result) == 1:
            return result[0][0]
        else:
            # try substring auto match
            query = "select substring, merchant_category from substring_auto_match"
            result = self.select(query)
            substring_matches = list()
            for item in result:
                if item[0] in merchant.lower():
                    substring_matches.append(item[1])
            if len(substring_matches) > 1:
                raise ValueError(
                    f"Multiple categories found for {merchant}. Something is wrong."
                )
            elif len(substring_matches) == 1:
                return substring_matches[0]
            else:
                return None

    def insert_into_auto_match(
        self, merchant: str, category: str, subcategory: str | None = None
    ) -> None:
        """
        Insert a new merchant into the auto_match table.
        """
        query = (
            "insert into auto_match (merchant_name, merchant_category) values (%s, %s)"
        )
        self.insert(query, (merchant, category))

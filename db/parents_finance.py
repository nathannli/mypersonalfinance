from collections.abc import Callable, Mapping
from dataclasses import replace
from datetime import date
from functools import partial

import polars as pl

from db.finance_base import FinanceDB
from services.deterministic_categorization import (
    DeterministicOutcome,
    resolve_parents_choice,
)
from services.enriched_categorization import (
    PacketResolution,
    resolve_approved_packet,
)
from services.llm_categorizer import OpenCodexCategorizer
from services.manual_etransfers import needs_manual_context
from services.research_packets import CategorySuggestion, record_suggestion
from services.transaction_categorization import (
    Resolution,
    TransactionOutcome,
    TransactionStatus,
    UnresolvedReason,
    build_canonical_context,
    normalize_context_text,
)


class ParentsFinanceDB(FinanceDB):
    choice_id_field = "category_id"
    cron: bool

    # Taxonomy snapshot: read once per run (V43). Class-level None default
    # so subclasses that skip __init__ still observe the lazy cache.
    _categorization_choices: list[dict[str, object]] | None = None

    def __init__(
        self,
        debug: bool = False,
        cron: bool = False,
        packet_resolver: Callable[[str], PacketResolution] | None = None,
        suggestion_recorder: Callable[[CategorySuggestion], CategorySuggestion]
        | None = None,
    ):
        super().__init__(database_name="parents_finance", debug=debug)
        self.cron = cron
        # Both default to this database's private research state, never finance's.
        self._packet_resolver = packet_resolver or partial(
            resolve_approved_packet, database="parents_finance"
        )
        self._suggestion_recorder = suggestion_recorder or partial(
            record_suggestion, database="parents_finance"
        )

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

    def _insert_choice(
        self,
        date: date,
        merchant: str,
        cost: float,
        choice: Mapping[str, object],
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

    def insert_manual_transfer(
        self,
        date: date,
        merchant: str,
        cost: float,
        category_id: int,
    ) -> TransactionOutcome:
        """Save the user's choice for one held row, without learning a rule."""
        if not needs_manual_context(
            normalize_context_text(merchant), "parents_finance"
        ):
            raise ValueError(
                "Manual categorization requires a transfer, cheque or cash row"
            )
        if isinstance(category_id, bool) or not isinstance(category_id, int):
            raise ValueError("category_id must be an integer")
        choice = next(
            (
                row
                for row in self.get_categorization_choices()
                if row["category_id"] == category_id
            ),
            None,
        )
        if choice is None:
            raise ValueError(f"Unknown category_id: {category_id}")
        if self.check_if_expense_exists(date, merchant, cost):
            return TransactionOutcome(TransactionStatus.DUPLICATE, Resolution.MANUAL)
        return self._insert_choice(date, merchant, cost, choice, Resolution.MANUAL)

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
        deterministic = resolve_parents_choice(
            cc_category=cc_category,
            choices=choices,
            auto_match=partial(self.get_auto_match_category, merchant),
        )
        if deterministic.outcome is DeterministicOutcome.INVALID_MAPPING:
            # V44: a stale or ambiguous mapping is surfaced deterministically,
            # never silently rerouted to the LLM path.
            return TransactionOutcome(
                TransactionStatus.UNRESOLVED,
                Resolution.DETERMINISTIC,
                UnresolvedReason.INVALID_CHOICE,
            )
        if deterministic.outcome is DeterministicOutcome.MATCHED:
            assert deterministic.choice is not None
            return self._insert_choice(
                date, merchant, cost, deterministic.choice, Resolution.DETERMINISTIC
            )

        if needs_manual_context(normalize_context_text(merchant), "parents_finance"):
            return TransactionOutcome(
                TransactionStatus.UNRESOLVED,
                reason=UnresolvedReason.MANUAL_CONTEXT_REQUIRED,
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

        # Packet state surfaces even with no provider client, and an unusable
        # packet never falls back to unenriched categorization.
        resolution = self._packet_resolver(context.merchant)
        if resolution.packet is None:
            return TransactionOutcome(
                TransactionStatus.UNRESOLVED,
                reason=resolution.reason,
            )
        packet = resolution.packet
        context = replace(
            context,
            research_packet_sha256=packet.packet_sha256,
            research_packet_schema_version=packet.schema_version,
            research_packet_query_version=packet.query_version,
        )

        if categorizer is None:
            return TransactionOutcome(
                TransactionStatus.UNRESOLVED,
                reason=UnresolvedReason.PROVIDER_ERROR,
            )

        execution = categorizer.categorize_enriched(context, packet)
        if execution.decision is None:
            return TransactionOutcome(
                TransactionStatus.UNRESOLVED,
                reason=execution.reason or UnresolvedReason.MALFORMED,
            )
        return self._apply_enriched_decision(
            date,
            merchant,
            cost,
            choices,
            context,
            packet,
            execution.decision,
            execution.source or categorizer,
        )

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
            return self.get_substring_auto_match_category(merchant)

    def get_substring_auto_match_category(self, merchant: str) -> str | None:
        """
        Get the category from the substring rules alone.
        """
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

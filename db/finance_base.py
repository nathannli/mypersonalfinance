from abc import abstractmethod
from collections.abc import Callable
from datetime import date
from typing import Any

from db.base import PostgresDB
from services.llm_categorizer import (
    EnrichedAbstain,
    EnrichedDecision,
    EnrichedSelect,
    EnrichedSuggestion,
    OpenCodexCategorizer,
)
from services.research_packets import CategorySuggestion, ResearchPacket
from services.transaction_categorization import (
    CanonicalContext,
    Resolution,
    TransactionOutcome,
    TransactionStatus,
    UnresolvedReason,
)


class FinanceDB(PostgresDB):
    # The key naming a taxonomy choice in this database's offered choices.
    choice_id_field: str
    _suggestion_recorder: Callable[[CategorySuggestion], CategorySuggestion]

    def __init__(self, database_name: str, debug: bool = False):
        super().__init__(database_name=database_name, debug=debug)

    @abstractmethod
    def insert_expense(self, *_: Any, **__: Any) -> TransactionOutcome:
        """Insert one expense or return its non-insert outcome."""
        return TransactionOutcome(TransactionStatus.IGNORED)

    @abstractmethod
    def get_auto_match_category(self, merchant: str) -> Any:
        """
        Get the category and subcategory for the merchant.
        """
        return

    @abstractmethod
    def insert_into_auto_match(
        self, merchant: str, category: str, subcategory: str
    ) -> None:
        """
        Insert a new merchant into the auto_match table.
        """
        return

    def _check_exists(self, table: str, filters: dict[str, Any]) -> bool:
        """
        Generic method to check if a row exists in the database table.
        """
        conditions = " AND ".join(f"{key} = %s" for key in filters)
        query = f"SELECT id FROM {table} WHERE {conditions}"
        params = tuple(filters.values())
        return len(self.select(query, params)) > 0

    def check_if_expense_exists(self, date: date, merchant: str, cost: float) -> bool:
        return self._check_exists(
            "expenses", {"date": date, "merchant": merchant, "cost": cost}
        )

    def get_expense_id(self, date: date, merchant: str, cost: float) -> int:
        """
        Get the id of an expense in the database.
        """
        query = (
            "select id from expenses where date = %s and merchant = %s and cost = %s"
        )
        result = self.select(query, (date, merchant, cost))
        return result[0][0]

    def delete_expense(self, expense_id: int) -> None:
        """
        Delete an expense from the database.
        """
        query = "delete from expenses where id = %s"
        self.insert(query, (expense_id,))

    def _apply_enriched_decision(
        self,
        date: date,
        merchant: str,
        cost: float,
        choices: list[dict[str, object]],
        context: CanonicalContext,
        packet: ResearchPacket,
        decision: EnrichedDecision,
        categorizer: OpenCodexCategorizer | None = None,
    ) -> TransactionOutcome:
        """Map one validated enriched decision to exactly one outcome (V25)."""
        if isinstance(decision, EnrichedAbstain):
            return TransactionOutcome(
                TransactionStatus.UNRESOLVED,
                Resolution.LLM,
                UnresolvedReason.ABSTAINED,
            )

        if isinstance(decision, EnrichedSuggestion):
            # V19/V21/V22: review-only. The proposal is persisted before the
            # outcome is reported, so a row is never counted as suggested
            # without a stored grouped proposal; a store failure propagates
            # rather than reporting an unpersisted suggestion.
            suggestion = CategorySuggestion(
                normalized_merchant=context.merchant,
                category_name=decision.category_name,
                subcategory_name=decision.subcategory_name,
                parent_category_id=decision.parent_category_id,
                rationale=decision.rationale,
                evidence_urls=decision.evidence_urls,
                research_packet_sha256=packet.packet_sha256,
                context_fingerprints=(context.fingerprint,),
            )
            stored = self._suggestion_recorder(suggestion)
            return TransactionOutcome(
                TransactionStatus.SUGGESTED,
                Resolution.LLM,
                suggestion_id=stored.suggestion_id,
            )

        assert isinstance(decision, EnrichedSelect)
        authorized_choice = next(
            (
                item
                for item in choices
                if item[self.choice_id_field] == decision.choice_id
            ),
            None,
        )
        if authorized_choice is None:
            return TransactionOutcome(
                TransactionStatus.UNRESOLVED,
                Resolution.LLM,
                UnresolvedReason.INVALID_CHOICE,
            )
        # V33: write mode authorizes only an exact approved select for this
        # context, packet hash and choice. Every other selection stays shadow.
        if categorizer is not None and categorizer.can_write_enriched(
            context, packet, decision.choice_id
        ):
            return self._insert_choice(
                date, merchant, cost, authorized_choice, Resolution.LLM
            )
        return TransactionOutcome(
            TransactionStatus.SHADOW,
            Resolution.LLM,
            suggested_choice_id=decision.choice_id,
        )

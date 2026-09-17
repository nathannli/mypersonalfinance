from collections.abc import Callable, Mapping
from dataclasses import replace
from datetime import date
from functools import partial

import polars as pl

from sources.ref_data import reimbursement_merchant_ref
from db.finance_base import FinanceDB
from services.deterministic_categorization import (
    DeterministicOutcome,
    find_exact_auto_match,
    find_substring_auto_match,
    resolve_deterministic_choice,
)
from services.enriched_categorization import (
    PacketResolution,
    resolve_approved_packet,
)
from services.llm_categorizer import (
    EnrichedAbstain,
    EnrichedDecision,
    EnrichedSelect,
    EnrichedSuggestion,
    OpenCodexCategorizer,
)
from services.research_packets import (
    CategorySuggestion,
    ResearchPacket,
    record_suggestion,
)
from services.transaction_categorization import (
    CanonicalContext,
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

    def __init__(
        self,
        debug: bool = False,
        packet_resolver: Callable[[str], PacketResolution] | None = None,
        suggestion_recorder: Callable[[CategorySuggestion], CategorySuggestion]
        | None = None,
    ):
        super().__init__(database_name="finance", debug=debug)
        # V15/V47: the frozen-packet gate is injectable so tests can exercise
        # packet states without a live store root. Defaults to the read-only
        # resolver, which writes nothing and never calls TinyFish.
        self._packet_resolver = packet_resolver or resolve_approved_packet
        # V22/V24: the grouped-suggestion writer is injectable the same way so
        # tests never touch the repository-root private artifact.
        self._suggestion_recorder = suggestion_recorder or record_suggestion

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

    def _insert_choice(
        self,
        date: date,
        merchant: str,
        cost: float,
        choice: Mapping[str, object],
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
        deterministic = resolve_deterministic_choice(
            card_type=card_type,
            cc_category=cc_category,
            choices=choices,
            auto_match=partial(self.get_auto_match_category, merchant),
        )
        if deterministic.outcome is DeterministicOutcome.INVALID_MAPPING:
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

        # The frozen packet gates the enriched path. Its state reasons surface
        # even with no provider client configured, and a packet that is missing,
        # stale, tampered or unapproved never falls back to unenriched
        # categorization (V15, V25, V55).
        resolution = self._packet_resolver(context.merchant)
        if resolution.packet is None:
            return TransactionOutcome(
                TransactionStatus.UNRESOLVED,
                reason=resolution.reason,
            )
        packet = resolution.packet

        # Bind the approved packet into the context *after* resolution: the
        # fingerprint shared by the cache, the gold set and the approval record
        # is the packet-bound one (V30), and the categorizer refuses any
        # context whose packet identity does not match the packet it is given.
        # The unbound context above still owns amount validation and the
        # normalized-merchant lookup, so both happen exactly once.
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
            categorizer,
        )

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
            (item for item in choices if item["subcategory_id"] == decision.choice_id),
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

    def get_auto_match_category(self, merchant: str) -> tuple[str, str] | None:
        """
        Get the category and subcategory for the merchant.
        """
        query = "select merchant_category, merchant_subcategory from merchant_name_auto_match where merchant_name = %s"
        exact = find_exact_auto_match(merchant, self.select(query, (merchant,)))
        if exact is not None:
            return exact
        # try substring auto match
        query = "select substring, merchant_category, merchant_subcategory from substring_auto_match"
        return find_substring_auto_match(merchant, self.select(query))

    def insert_into_auto_match(
        self, merchant: str, category: str, subcategory: str
    ) -> None:
        """
        Insert a new merchant into the auto_match table.
        """
        query = "insert into merchant_name_auto_match (merchant_name, merchant_category, merchant_subcategory) values (%s, %s, %s)"
        self.insert(query, (merchant, category, subcategory))

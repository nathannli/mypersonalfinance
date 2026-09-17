"""Shared construction of the transaction categorizer.

Both entry points (`load-transactions.py` via the CLI and
`load-excel-transactions.py`) build the categorizer here so write-mode
gating and provider configuration have exactly one implementation.
"""

from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass

from config import Config
from services.llm_categorizer import OpenCodexCategorizer, OpenCodexConfig
from services.research_packets import ResearchPacket
from services.transaction_categorization import CanonicalContext
from services.transaction_llm_approval import (
    ENRICHED_GOLD_DATABASE,
    authorize_enriched_write_mode,
    authorize_write_mode,
)

WriteAuthorizer = Callable[[CanonicalContext, int], bool]
EnrichedWriteAuthorizer = Callable[[CanonicalContext, ResearchPacket, int], bool]


@dataclass(frozen=True)
class WriteAuthorizers:
    """The write gates one entry point resolved for one database (V33)."""

    legacy: WriteAuthorizer | None = None
    enriched: EnrichedWriteAuthorizer | None = None


def build_categorizer(
    config: Config,
    write_authorizer: WriteAuthorizer | None = None,
    enriched_write_authorizer: EnrichedWriteAuthorizer | None = None,
    *,
    model: str | None = None,
) -> OpenCodexCategorizer:
    """Build a categorizer bound to the configured OpenCodex proxy.

    ``model`` selects the exact requested model ID; ``None`` keeps the
    unenriched ``TRANSACTION_LLM_MODEL``. The enriched ``finance`` path passes
    ``ENRICHED_TRANSACTION_LLM_MODEL`` so request bytes, cache identity, gold
    validation, and the approval fingerprint all bind one and the same model
    (V51).
    """
    return OpenCodexCategorizer(
        OpenCodexConfig(
            base_url=config.opencodex_base_url,
            api_key=config.opencodex_api_key,
            model=config.transaction_llm_model if model is None else model,
            timeout_seconds=config.transaction_llm_timeout_seconds,
            mode=config.transaction_llm_mode,
        ),
        write_authorizer=write_authorizer,
        enriched_write_authorizer=enriched_write_authorizer,
    )


def write_authorizer_for(
    config: Config,
    database: str,
    choices_provider: Callable[[], Sequence[Mapping[str, object]]],
) -> WriteAuthorizer | None:
    """Resolve the write authorizer, failing closed before any DB mutation.

    Shadow mode needs no authorization and never reads the taxonomy.
    Explicit write mode raises ``ApprovalError`` when approval is missing
    or stale; it is never silently downgraded to shadow.
    """
    if config.transaction_llm_mode != "write":
        return None
    choices: Iterable[Mapping[str, object]] = choices_provider()
    return authorize_write_mode(
        database=database,
        base_url=config.opencodex_base_url,
        model=config.transaction_llm_model,
        choices=choices,
    )


def enriched_write_authorizer_for(
    config: Config,
    database: str,
    choices_provider: Callable[[], Sequence[Mapping[str, object]]],
    *,
    packet_resolver: Callable[[str], object],
) -> EnrichedWriteAuthorizer | None:
    """Resolve the packet-bound write authorizer, failing closed (V33).

    Shadow mode returns ``None`` without reading the taxonomy, so a shadow run
    never needs packet approval. The fingerprint binds the enriched model,
    because that is the exact model this path calls (V51).
    """
    if config.transaction_llm_mode != "write":
        return None
    return authorize_enriched_write_mode(
        database=database,
        base_url=config.opencodex_base_url,
        model=config.enriched_transaction_llm_model,
        choices_provider=choices_provider,
        packet_resolver=packet_resolver,
    )


def write_authorizers_for(
    config: Config,
    database: str,
    choices_provider: Callable[[], Sequence[Mapping[str, object]]],
    *,
    packet_resolver: Callable[[str], object],
) -> WriteAuthorizers:
    """Resolve both write gates for one database, failing closed before mutation.

    Enriched ``finance`` is gated only by packet-bound approval: its unknown rows
    are answered from frozen packet evidence, so the unenriched approval does not
    govern them and is not required for the run to start (V33).
    ``parents_finance`` keeps the unenriched gate unchanged (V2).
    """
    if database == ENRICHED_GOLD_DATABASE:
        return WriteAuthorizers(
            enriched=enriched_write_authorizer_for(
                config,
                database,
                choices_provider,
                packet_resolver=packet_resolver,
            )
        )
    return WriteAuthorizers(
        legacy=write_authorizer_for(config, database, choices_provider)
    )

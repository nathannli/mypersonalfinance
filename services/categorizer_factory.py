"""Shared construction of the transaction categorizer.

Both entry points (`load-transactions.py` via the CLI and
`load-excel-transactions.py`) build the categorizer here so write-mode
gating and provider configuration have exactly one implementation.
"""

from collections.abc import Callable, Iterable, Mapping, Sequence

from config import Config
from services.llm_categorizer import OpenCodexCategorizer, OpenCodexConfig
from services.transaction_categorization import CanonicalContext
from services.transaction_llm_approval import authorize_write_mode

WriteAuthorizer = Callable[[CanonicalContext, int], bool]


def build_categorizer(
    config: Config, write_authorizer: WriteAuthorizer | None = None
) -> OpenCodexCategorizer:
    """Build a categorizer bound to the configured OpenCodex proxy."""
    return OpenCodexCategorizer(
        OpenCodexConfig(
            base_url=config.opencodex_base_url,
            api_key=config.opencodex_api_key,
            model=config.transaction_llm_model,
            timeout_seconds=config.transaction_llm_timeout_seconds,
            mode=config.transaction_llm_mode,
        ),
        write_authorizer=write_authorizer,
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

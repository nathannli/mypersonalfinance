"""Pure deterministic (non-LLM) categorization resolution.

Shared by the transaction-load path and the research path so both decide
"already known" versus "unknown" from exactly one implementation (V1, V2).

This module performs no database access of its own: callers read the live
taxonomy and the auto-match tables and pass them in, which keeps every rule
here testable without a database and makes the research runner structurally
incapable of mutating anything (V4).
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from enum import StrEnum

from sources.csv.rogers import RogersStatement
from sources.csv.simplii_visa import SimpliiVisaStatement

ROGERS_CARD_TYPE = "rogers"
SIMPLII_VISA_CARD_TYPE = "simplii_visa"


class DeterministicOutcome(StrEnum):
    """The three ways one deterministic resolution attempt can end."""

    MATCHED = "matched"
    NO_MATCH = "no_match"
    INVALID_MAPPING = "invalid_mapping"


@dataclass(frozen=True)
class DeterministicResolution:
    """One resolution attempt: a live choice, no mapping, or a broken one.

    ``INVALID_MAPPING`` is distinct from ``NO_MATCH`` on purpose. A mapping
    exists but the live taxonomy cannot honour it, so the row must not fall
    through to a packet lookup or an LLM request: guessing would silently
    mis-categorize a row whose mapping a human needs to fix.
    """

    outcome: DeterministicOutcome
    choice: Mapping[str, object] | None = None

    def __post_init__(self) -> None:
        matched = self.outcome is DeterministicOutcome.MATCHED
        if matched and self.choice is None:
            raise ValueError("a matched resolution requires a choice")
        if not matched and self.choice is not None:
            raise ValueError("only a matched resolution carries a choice")


def find_exact_auto_match(
    merchant: str, rows: Sequence[tuple[str, str]]
) -> tuple[str, str] | None:
    """The single exact merchant mapping, or ``None`` when there is none.

    Two rows for one exact merchant name means the table itself is
    inconsistent, so this raises rather than picking one.
    """
    if len(rows) > 1:
        raise ValueError(
            f"Multiple categories found for {merchant}. Something is wrong."
        )
    if not rows:
        return None
    return (rows[0][0], rows[0][1])


def find_substring_auto_match(
    merchant: str, rows: Sequence[tuple[str, str, str]]
) -> tuple[str, str] | None:
    """The first ``(substring, category, subcategory)`` rule that matches.

    The stored pattern is compared as written, against a lowercased merchant
    only, because the rows are authored lowercase. Several matching rules
    resolve to the first row rather than raising, which is the established
    behaviour of this table.
    """
    lowered = merchant.lower()
    for pattern, category, subcategory in rows:
        if pattern in lowered:
            return (category, subcategory)
    return None


def find_reference_choice(
    choices: Sequence[Mapping[str, object]], reference: tuple[str, str]
) -> Mapping[str, object] | None:
    """The one live choice matching both the category and subcategory names.

    Matching the subcategory name alone is not enough: the live taxonomy holds
    two subcategories named ``Misc`` (under both Shopping and Misc), so a
    subcategory-only match would silently pick the wrong category.
    """
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


def resolve_statement_reference(
    card_type: str,
    cc_category: str | None,
    *,
    auto_match: Callable[[], tuple[str, str] | None],
) -> tuple[str, str] | None:
    """The statement-specific category reference, falling back to auto-match.

    ``auto_match`` is the caller's merchant-table lookup, invoked at most once
    and only when the statement itself does not supply an answer, so the
    underlying reads keep their existing order and cost.
    """
    if card_type == ROGERS_CARD_TYPE and cc_category is not None:
        return RogersStatement.auto_match_category(cc_category) or auto_match()
    if card_type == SIMPLII_VISA_CARD_TYPE:
        # This card is only ever used for restaurants.
        return SimpliiVisaStatement.auto_match_category()
    return auto_match()


def resolve_deterministic_choice(
    *,
    card_type: str,
    cc_category: str | None,
    choices: Sequence[Mapping[str, object]],
    auto_match: Callable[[], tuple[str, str] | None],
) -> DeterministicResolution:
    """Resolve one row deterministically, before any packet or LLM work (V1).

    Any ``ValueError`` raised while deriving the reference (an ambiguous exact
    merchant mapping, or a statement reference table that has become
    inconsistent) is an invalid mapping rather than a no-match, because a
    mapping does exist and simply cannot be honoured.
    """
    try:
        reference = resolve_statement_reference(
            card_type, cc_category, auto_match=auto_match
        )
    except ValueError:
        return DeterministicResolution(DeterministicOutcome.INVALID_MAPPING)

    if reference is None:
        return DeterministicResolution(DeterministicOutcome.NO_MATCH)

    choice = find_reference_choice(choices, reference)
    if choice is None:
        return DeterministicResolution(DeterministicOutcome.INVALID_MAPPING)
    return DeterministicResolution(DeterministicOutcome.MATCHED, choice)

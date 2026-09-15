from __future__ import annotations

import hashlib
import json
import unicodedata
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from enum import StrEnum
from typing import Any, Iterable, Mapping


class TransactionStatus(StrEnum):
    INSERTED = "inserted"
    DUPLICATE = "duplicate"
    IGNORED = "ignored"
    DELETED = "deleted"
    UNRESOLVED = "unresolved"
    SHADOW = "shadow"


class Resolution(StrEnum):
    DETERMINISTIC = "deterministic"
    LLM = "llm"
    NONE = "none"


class UnresolvedReason(StrEnum):
    ABSTAINED = "abstained"
    TIMEOUT = "timeout"
    PROVIDER_ERROR = "provider_error"
    MALFORMED = "malformed"
    INVALID_CHOICE = "invalid_choice"
    CIRCUIT_OPEN = "circuit_open"


class ProviderAction(StrEnum):
    SELECT = "select"
    ABSTAIN = "abstain"
    UNRESOLVED = "unresolved"


@dataclass(frozen=True)
class TransactionOutcome:
    status: TransactionStatus
    resolution: Resolution = Resolution.NONE
    reason: UnresolvedReason | None = None
    suggested_choice_id: int | None = None


@dataclass(frozen=True)
class CategorizationResult:
    action: ProviderAction
    choice_id: int | None = None
    reason: UnresolvedReason | None = None
    context_fingerprint: str | None = None


@dataclass(frozen=True)
class CanonicalContext:
    database: str
    merchant: str
    amount_minor_units: int
    statement_category: str | None
    allowed_choices: tuple[dict[str, int | str], ...]

    def as_dict(self) -> dict[str, Any]:
        return {
            "allowed_choices": list(self.allowed_choices),
            "amount_minor_units": self.amount_minor_units,
            "database": self.database,
            "merchant": self.merchant,
            "statement_category": self.statement_category,
        }

    def to_bytes(self) -> bytes:
        return json.dumps(
            self.as_dict(), ensure_ascii=False, separators=(",", ":"), sort_keys=True
        ).encode("utf-8")

    @property
    def fingerprint(self) -> str:
        return hashlib.sha256(self.to_bytes()).hexdigest()


def normalize_context_text(value: str) -> str:
    normalized = unicodedata.normalize("NFKC", value).replace("\xa0", " ")
    return " ".join(normalized.split()).strip().casefold()


def normalize_optional_context_text(value: str | None) -> str | None:
    if value is None:
        return None
    normalized = normalize_context_text(value)
    return normalized or None


def _normalize_choice_label(value: object, field: str) -> str:
    if not isinstance(value, str):
        raise ValueError(f"{field} must be a string")
    normalized = unicodedata.normalize("NFKC", value).replace("\xa0", " ")
    normalized = " ".join(normalized.split()).strip()
    if not normalized:
        raise ValueError(f"{field} must not be blank")
    return normalized


def _choice_id(value: object, field: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(f"{field} must be an integer")
    return value


def canonicalize_choices(
    database: str, choices: Iterable[Mapping[str, object]]
) -> tuple[dict[str, int | str], ...]:
    canonical: list[dict[str, int | str]] = []
    seen_ids: set[int] = set()

    for choice in choices:
        if database == "finance":
            expected = {
                "subcategory_id",
                "category_id",
                "subcategory_name",
                "category_name",
            }
            if set(choice) != expected:
                raise ValueError("finance choice has invalid fields")
            subcategory_id = _choice_id(choice["subcategory_id"], "subcategory_id")
            category_id = _choice_id(choice["category_id"], "category_id")
            if subcategory_id in seen_ids:
                raise ValueError("choice IDs must be unique")
            seen_ids.add(subcategory_id)
            canonical.append(
                {
                    "category_id": category_id,
                    "category_name": _normalize_choice_label(
                        choice["category_name"], "category_name"
                    ),
                    "subcategory_id": subcategory_id,
                    "subcategory_name": _normalize_choice_label(
                        choice["subcategory_name"], "subcategory_name"
                    ),
                }
            )
        elif database == "parents_finance":
            expected = {"category_id", "category_name"}
            if set(choice) != expected:
                raise ValueError("parents_finance choice has invalid fields")
            category_id = _choice_id(choice["category_id"], "category_id")
            if category_id in seen_ids:
                raise ValueError("choice IDs must be unique")
            seen_ids.add(category_id)
            canonical.append(
                {
                    "category_id": category_id,
                    "category_name": _normalize_choice_label(
                        choice["category_name"], "category_name"
                    ),
                }
            )
        else:
            raise ValueError(f"Unsupported database: {database}")

    if not canonical:
        raise ValueError("allowed choices must not be empty")

    id_field = "subcategory_id" if database == "finance" else "category_id"

    def choice_sort_key(row: dict[str, int | str]) -> int:
        value = row[id_field]
        if isinstance(value, bool) or not isinstance(value, int):
            raise AssertionError("canonical choice ID must be an integer")
        return value

    return tuple(sorted(canonical, key=choice_sort_key))


def amount_to_minor_units(amount: Decimal | float | int | str) -> int:
    try:
        decimal_amount = Decimal(str(amount))
    except (InvalidOperation, ValueError) as exc:
        raise ValueError("amount must be a finite decimal") from exc

    if not decimal_amount.is_finite():
        raise ValueError("amount must be a finite decimal")

    minor_units = decimal_amount * 100
    if minor_units != minor_units.to_integral_value():
        raise ValueError("amount must be exact to one cent")
    try:
        return int(minor_units)
    except (OverflowError, ValueError) as exc:
        raise ValueError("amount must be a finite decimal") from exc


def build_canonical_context(
    *,
    database: str,
    merchant: str,
    amount: Decimal | float | int | str,
    statement_category: str | None,
    allowed_choices: Iterable[Mapping[str, object]],
) -> CanonicalContext:
    if not isinstance(merchant, str):
        raise ValueError("merchant must be a string")
    normalized_merchant = normalize_context_text(merchant)
    if not normalized_merchant:
        raise ValueError("merchant must not be blank")

    return CanonicalContext(
        database=database,
        merchant=normalized_merchant,
        amount_minor_units=amount_to_minor_units(amount),
        statement_category=normalize_optional_context_text(statement_category),
        allowed_choices=canonicalize_choices(database, allowed_choices),
    )

from __future__ import annotations

import hashlib
import json
import re
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
    SUGGESTED = "suggested"


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
    INVALID_CONTEXT = "invalid_context"
    CIRCUIT_OPEN = "circuit_open"
    # Research (web enrichment) reasons. The first block is produced while
    # researching a merchant; the second block is a load-time or review-time
    # classification and can never be stored as a packet failure reason (V55).
    RESEARCH_AUTH = "research_auth"
    RESEARCH_RATE_LIMIT = "research_rate_limit"
    RESEARCH_TIMEOUT = "research_timeout"
    RESEARCH_PROVIDER_ERROR = "research_provider_error"
    RESEARCH_NO_RESULTS = "research_no_results"
    RESEARCH_NO_VALID_URLS = "research_no_valid_urls"
    RESEARCH_FETCH_FAILED = "research_fetch_failed"
    RESEARCH_EMPTY_EVIDENCE = "research_empty_evidence"
    RESEARCH_IRRELEVANT = "research_irrelevant"
    RESEARCH_MALFORMED = "research_malformed"
    RESEARCH_MISSING = "research_missing"
    RESEARCH_STALE = "research_stale"
    RESEARCH_TAMPERED = "research_tampered"
    RESEARCH_UNAPPROVED = "research_unapproved"


class ProviderAction(StrEnum):
    SELECT = "select"
    SUGGEST_NEW = "suggest_new"
    ABSTAIN = "abstain"
    UNRESOLVED = "unresolved"


@dataclass(frozen=True)
class TransactionOutcome:
    status: TransactionStatus
    resolution: Resolution = Resolution.NONE
    reason: UnresolvedReason | None = None
    suggested_choice_id: int | None = None
    suggestion_id: str | None = None


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
    # Only enriched `finance` contexts carry packet identity (V30). These keys
    # are omitted from the canonical bytes when absent so unenriched and
    # `parents_finance` fingerprints stay byte-identical (V2), while enriched
    # contexts get a distinct fingerprint space (V46). The triple is
    # all-or-none: a packet digest always travels with that packet's schema and
    # query versions, because a digest without its versions cannot be re-bound
    # to the evidence it was approved against.
    research_packet_sha256: str | None = None
    research_packet_schema_version: str | None = None
    research_packet_query_version: str | None = None

    def as_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "allowed_choices": list(self.allowed_choices),
            "amount_minor_units": self.amount_minor_units,
            "database": self.database,
            "merchant": self.merchant,
            "statement_category": self.statement_category,
        }
        if self.research_packet_sha256 is not None:
            payload["research_packet_sha256"] = self.research_packet_sha256
        if self.research_packet_schema_version is not None:
            payload["research_packet_schema_version"] = (
                self.research_packet_schema_version
            )
        if self.research_packet_query_version is not None:
            payload["research_packet_query_version"] = (
                self.research_packet_query_version
            )
        return payload

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


_PACKET_SHA256_PATTERN = re.compile(r"\A[0-9a-f]{64}\Z")
_PACKET_VERSION_PATTERN = re.compile(r"\A[A-Za-z0-9._-]{1,64}\Z")


def require_packet_sha256(value: object) -> str:
    """Return a validated lowercase 64-hex research packet digest."""
    if not isinstance(value, str) or not _PACKET_SHA256_PATTERN.match(value):
        raise ValueError(
            "research_packet_sha256 must be a 64-character lowercase hex digest"
        )
    return value


def require_packet_version(value: object, field: str) -> str:
    """Return a validated packet schema or query version label."""
    if not isinstance(value, str) or not _PACKET_VERSION_PATTERN.match(value):
        raise ValueError(f"{field} must be a short version label")
    return value


def require_packet_identity(
    sha256: object, schema_version: object, query_version: object
) -> tuple[str, str, str]:
    """Validate packet identity as an all-or-none triple (V30).

    Returns the validated ``(sha256, schema_version, query_version)`` triple.
    A digest without its versions is rejected rather than partially accepted,
    so approval can never bind evidence whose version it cannot restate.
    """
    if sha256 is None:
        if schema_version is not None or query_version is not None:
            raise ValueError(
                "packet identity is all-or-none: packet versions require a packet "
                "digest"
            )
        raise ValueError("packet identity is all-or-none: packet digest is required")
    return (
        require_packet_sha256(sha256),
        require_packet_version(schema_version, "research_packet_schema_version"),
        require_packet_version(query_version, "research_packet_query_version"),
    )


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
    research_packet_sha256: str | None = None,
    research_packet_schema_version: str | None = None,
    research_packet_query_version: str | None = None,
) -> CanonicalContext:
    if not isinstance(merchant, str):
        raise ValueError("merchant must be a string")
    normalized_merchant = normalize_context_text(merchant)
    if not normalized_merchant:
        raise ValueError("merchant must not be blank")

    packet_sha256: str | None = None
    packet_schema_version: str | None = None
    packet_query_version: str | None = None
    if (
        research_packet_sha256 is not None
        or research_packet_schema_version is not None
        or research_packet_query_version is not None
    ):
        (
            packet_sha256,
            packet_schema_version,
            packet_query_version,
        ) = require_packet_identity(
            research_packet_sha256,
            research_packet_schema_version,
            research_packet_query_version,
        )

    return CanonicalContext(
        database=database,
        merchant=normalized_merchant,
        amount_minor_units=amount_to_minor_units(amount),
        statement_category=normalize_optional_context_text(statement_category),
        allowed_choices=canonicalize_choices(database, allowed_choices),
        research_packet_sha256=packet_sha256,
        research_packet_schema_version=packet_schema_version,
        research_packet_query_version=packet_query_version,
    )

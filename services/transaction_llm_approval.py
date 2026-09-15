"""Write-approval gate for LLM-selected categories.

Explicit write mode is authorized per database only, and only when the
locally stored approval record still matches the exact runtime identity:
database, normalized base URL, exact model ID, canonical prompt bytes,
actual response-schema bytes, that database's live taxonomy, and that
database's private gold subset. Any mismatch aborts before a database
mutation; it never silently downgrades to shadow.

Neither this module nor the approval record stores API keys, raw provider
payloads, merchants, amounts, or expected choices.
"""

import hashlib
import json
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from services.llm_categorizer import (
    canonical_json_bytes,
    canonical_prompt_bytes,
    canonical_response_schema_bytes,
    normalize_base_url,
)
from services.transaction_categorization import (
    CanonicalContext,
    ProviderAction,
    canonicalize_choices,
    normalize_context_text,
    normalize_optional_context_text,
)
from utils.repo_paths import private_approval_path, private_gold_path

REQUIRED_PASSES = 3

SUPPORTED_DATABASES = ("finance", "parents_finance")


class ApprovalError(RuntimeError):
    """Raised when explicit write mode is not authorized."""


@dataclass(frozen=True)
class GoldCase:
    """One user-approved expectation for a single canonical context."""

    database: str
    fingerprint: str
    merchant: str
    amount_minor_units: int
    statement_category: str | None
    expected_action: str
    expected_choice_id: int | None

    def build_context(
        self, choices: Iterable[Mapping[str, object]]
    ) -> CanonicalContext:
        """Rebuild this case's canonical context against the live taxonomy."""
        context = CanonicalContext(
            database=self.database,
            merchant=self.merchant,
            amount_minor_units=self.amount_minor_units,
            statement_category=self.statement_category,
            allowed_choices=canonicalize_choices(self.database, choices),
        )
        if context.fingerprint != self.fingerprint:
            raise ApprovalError(
                "gold case fingerprint does not match the live taxonomy"
            )
        return context

    def matches(self, action: str, choice_id: int | None) -> bool:
        if action != self.expected_action:
            return False
        if self.expected_action == ProviderAction.SELECT:
            return choice_id is not None and choice_id == self.expected_choice_id
        return True


def sha256_hex(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def taxonomy_hash(database: str, choices: Iterable[Mapping[str, object]]) -> str:
    """Hash the live taxonomy exactly as the prompt would canonicalize it."""
    canonical = canonicalize_choices(database, choices)
    return sha256_hex(canonical_json_bytes(list(canonical)))


def _case_payload(case: GoldCase) -> dict[str, Any]:
    return {
        "amount_minor_units": case.amount_minor_units,
        "database": case.database,
        "expected_action": case.expected_action,
        "expected_choice_id": case.expected_choice_id,
        "fingerprint": case.fingerprint,
        "merchant": case.merchant,
        "statement_category": case.statement_category,
    }


def gold_subset_hash(cases: Iterable[GoldCase]) -> str:
    """Hash one database's gold subset in a stable order."""
    payload = sorted(
        (_case_payload(case) for case in cases),
        key=lambda item: item["fingerprint"],
    )
    return sha256_hex(canonical_json_bytes(payload))


def parse_gold_cases(document: Any, database: str) -> list[GoldCase]:
    """Read one database's gold subset, rejecting malformed or mixed data."""
    if not isinstance(document, Mapping):
        raise ApprovalError("gold set must be a JSON object")
    raw_cases = document.get("cases")
    if not isinstance(raw_cases, list):
        raise ApprovalError("gold set must contain a 'cases' list")

    cases: list[GoldCase] = []
    seen: set[str] = set()
    for raw in raw_cases:
        if not isinstance(raw, Mapping):
            raise ApprovalError("gold case must be an object")
        case_database = raw.get("database")
        if case_database not in SUPPORTED_DATABASES:
            raise ApprovalError("gold case must name a supported database")
        if case_database != database:
            continue

        fingerprint = raw.get("fingerprint")
        if not isinstance(fingerprint, str) or not fingerprint:
            raise ApprovalError("gold case requires a fingerprint")
        if fingerprint in seen:
            raise ApprovalError("gold case fingerprints must be unique per database")
        seen.add(fingerprint)

        action = raw.get("expected_action")
        if action not in (ProviderAction.SELECT, ProviderAction.ABSTAIN):
            raise ApprovalError("gold case action must be select or abstain")

        choice_id = raw.get("expected_choice_id")
        if action == ProviderAction.SELECT:
            if isinstance(choice_id, bool) or not isinstance(choice_id, int):
                raise ApprovalError("select gold case requires an integer choice ID")
        else:
            if choice_id is not None:
                raise ApprovalError("abstain gold case must not carry a choice ID")

        merchant = raw.get("merchant")
        if not isinstance(merchant, str) or not merchant:
            raise ApprovalError("gold case requires a merchant")

        amount = raw.get("amount_minor_units")
        if isinstance(amount, bool) or not isinstance(amount, int):
            raise ApprovalError("gold case requires integer amount_minor_units")

        statement_category = raw.get("statement_category")
        if statement_category is not None and not isinstance(statement_category, str):
            raise ApprovalError("gold case statement_category must be a string or null")

        cases.append(
            GoldCase(
                database=str(case_database),
                fingerprint=fingerprint,
                merchant=normalize_context_text(merchant),
                amount_minor_units=amount,
                statement_category=normalize_optional_context_text(statement_category),
                expected_action=str(action),
                expected_choice_id=choice_id
                if action == ProviderAction.SELECT
                else None,
            )
        )

    return cases


def load_gold_cases(database: str, path: Path | None = None) -> list[GoldCase]:
    gold_path = path or private_gold_path()
    if not gold_path.exists():
        raise ApprovalError(
            f"private gold set {gold_path.name} is missing; write mode is not approved"
        )
    try:
        document = json.loads(gold_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ApprovalError("private gold set could not be read") from exc
    return parse_gold_cases(document, database)


def build_fingerprints(
    database: str,
    base_url: str,
    model: str,
    choices: Iterable[Mapping[str, object]],
    cases: Iterable[GoldCase],
) -> dict[str, Any]:
    """Build the exact identity an approval record must match."""
    return {
        "base_url": normalize_base_url(base_url),
        "database": database,
        "gold_sha256": gold_subset_hash(cases),
        "model": model,
        "prompt_sha256": sha256_hex(canonical_prompt_bytes()),
        "schema_sha256": sha256_hex(canonical_response_schema_bytes()),
        "taxonomy_sha256": taxonomy_hash(database, choices),
    }


def load_approval_record(database: str, path: Path | None = None) -> Mapping[str, Any]:
    approval_path = path or private_approval_path()
    if not approval_path.exists():
        raise ApprovalError(
            f"approval record {approval_path.name} is missing; write mode is not approved"
        )
    try:
        document = json.loads(approval_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ApprovalError("approval record could not be read") from exc
    if not isinstance(document, Mapping):
        raise ApprovalError("approval record must be a JSON object")
    record = document.get(database)
    if not isinstance(record, Mapping):
        raise ApprovalError(
            f"approval record has no entry for database {database}; "
            "approval is per database and is never reused across databases"
        )
    return record


def verify_approval_record(
    record: Mapping[str, Any], expected: Mapping[str, Any]
) -> None:
    """Reject a missing, stale, or cross-database approval record."""
    for field, expected_value in expected.items():
        if record.get(field) != expected_value:
            raise ApprovalError(
                f"approval record is stale: {field} changed since approval"
            )
    passes = record.get("passes")
    if (
        isinstance(passes, bool)
        or not isinstance(passes, int)
        or passes < REQUIRED_PASSES
    ):
        raise ApprovalError(
            f"approval record requires {REQUIRED_PASSES} consecutive passing runs"
        )


def build_write_authorizer(cases: Iterable[GoldCase]):
    """Authorize writes only for approved contexts with the approved choice."""
    approved = {
        case.fingerprint: case
        for case in cases
        if case.expected_action == ProviderAction.SELECT
    }

    def authorize(context: CanonicalContext, choice_id: int) -> bool:
        case = approved.get(context.fingerprint)
        if case is None:
            return False
        return case.expected_choice_id == choice_id

    return authorize


def authorize_write_mode(
    database: str,
    base_url: str,
    model: str,
    choices: Iterable[Mapping[str, object]],
    gold_path: Path | None = None,
    approval_path: Path | None = None,
):
    """Return a write authorizer, or raise before any database mutation."""
    cases = load_gold_cases(database, gold_path)
    if not cases:
        raise ApprovalError(
            f"private gold set contains no cases for database {database}"
        )
    expected = build_fingerprints(database, base_url, model, choices, cases)
    record = load_approval_record(database, approval_path)
    verify_approval_record(record, expected)
    return build_write_authorizer(cases)

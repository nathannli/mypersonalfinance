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
import unicodedata
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from services.llm_categorizer import (
    EnrichedAbstain,
    EnrichedSelect,
    EnrichedSuggestion,
    canonical_enriched_prompt_bytes,
    canonical_enriched_response_schema_bytes,
    canonical_json_bytes,
    canonical_prompt_bytes,
    canonical_response_schema_bytes,
    normalize_base_url,
)
from services.research_packets import (
    MAX_SUGGESTION_NAME_CHARS,
    RESEARCH_QUERY_VERSION,
    RESEARCH_SCHEMA_VERSION,
)
from services.transaction_categorization import (
    CanonicalContext,
    ProviderAction,
    canonicalize_choices,
    normalize_context_text,
    normalize_optional_context_text,
    require_packet_sha256,
)
from utils.repo_paths import private_approval_path, private_gold_path

REQUIRED_PASSES = 3

SUPPORTED_DATABASES = ("finance", "parents_finance")

# Enrichment applies to `finance` only (V2).
ENRICHED_GOLD_DATABASE = "finance"

# One fixed exact case schema: every enriched case carries every field and the
# action decides which of them must be null. Unknown or missing fields fail
# parsing (V49).
ENRICHED_GOLD_CASE_FIELDS = frozenset(
    {
        "amount_minor_units",
        "database",
        "expected_action",
        "expected_category_name",
        "expected_choice_id",
        "expected_evidence_urls",
        "expected_parent_category_id",
        "expected_subcategory_name",
        "fingerprint",
        "merchant",
        "packet_query_version",
        "packet_schema_version",
        "research_packet_sha256",
        "statement_category",
    }
)

ENRICHED_GOLD_ACTIONS = (
    ProviderAction.SELECT,
    ProviderAction.SUGGEST_NEW,
    ProviderAction.ABSTAIN,
)

MIN_GOLD_EVIDENCE_URLS = 1
MAX_GOLD_EVIDENCE_URLS = 3

# Enriched approval is a separate protocol with a separate identity, so it gets
# its own record key. The unenriched `finance` entry is never reused or
# overwritten by the enriched gate, and vice versa (V33).
ENRICHED_APPROVAL_KEY = f"{ENRICHED_GOLD_DATABASE}:enriched"
ENRICHED_PROTOCOL = "enriched"
ENRICHED_AUTHORIZED_MODE = "write"


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


@dataclass(frozen=True)
class EnrichedGoldCase:
    """One user-approved expectation for a packet-bound canonical context.

    Enrichment applies to `finance` only (V2), so every enriched case binds an
    exact research packet hash plus that packet's schema and query versions
    (V30). A finance case with no packet identity is a pre-packet case and is
    rejected rather than reused (V46).
    """

    database: str
    fingerprint: str
    merchant: str
    amount_minor_units: int
    statement_category: str | None
    research_packet_sha256: str
    packet_schema_version: str
    packet_query_version: str
    expected_action: str
    expected_choice_id: int | None = None
    expected_category_name: str | None = None
    expected_subcategory_name: str | None = None
    expected_parent_category_id: int | None = None
    expected_evidence_urls: tuple[str, ...] = ()

    def build_context(
        self, choices: Iterable[Mapping[str, object]]
    ) -> CanonicalContext:
        """Rebuild this case's packet-bound context against the live taxonomy."""
        context = CanonicalContext(
            database=self.database,
            merchant=self.merchant,
            amount_minor_units=self.amount_minor_units,
            statement_category=self.statement_category,
            allowed_choices=canonicalize_choices(self.database, choices),
            research_packet_sha256=self.research_packet_sha256,
            research_packet_schema_version=self.packet_schema_version,
            research_packet_query_version=self.packet_query_version,
        )
        if context.fingerprint != self.fingerprint:
            raise ApprovalError(
                "enriched gold case fingerprint does not match its packet-bound context"
            )
        return context

    def matches(self, decision: object) -> bool:
        """Compare one enriched decision against the approved expectation."""
        if self.expected_action == ProviderAction.SELECT:
            return (
                isinstance(decision, EnrichedSelect)
                and decision.choice_id == self.expected_choice_id
            )
        if self.expected_action == ProviderAction.ABSTAIN:
            return isinstance(decision, EnrichedAbstain)
        if self.expected_action == ProviderAction.SUGGEST_NEW:
            return self._matches_suggestion(decision)
        return False

    def _matches_suggestion(self, decision: object) -> bool:
        if not isinstance(decision, EnrichedSuggestion):
            return False
        if decision.parent_category_id != self.expected_parent_category_id:
            return False
        if normalize_context_text(decision.category_name) != normalize_context_text(
            self.expected_category_name or ""
        ):
            return False
        if normalize_context_text(decision.subcategory_name) != normalize_context_text(
            self.expected_subcategory_name or ""
        ):
            return False
        # Citations are an unordered evidence set; membership must match
        # exactly. Both sides are already validated for bounds and uniqueness.
        return frozenset(decision.evidence_urls) == frozenset(
            self.expected_evidence_urls
        )

    def as_payload(self) -> dict[str, Any]:
        return {
            "amount_minor_units": self.amount_minor_units,
            "database": self.database,
            "expected_action": self.expected_action,
            "expected_category_name": self.expected_category_name,
            "expected_choice_id": self.expected_choice_id,
            "expected_evidence_urls": list(self.expected_evidence_urls),
            "expected_parent_category_id": self.expected_parent_category_id,
            "expected_subcategory_name": self.expected_subcategory_name,
            "fingerprint": self.fingerprint,
            "merchant": self.merchant,
            "packet_query_version": self.packet_query_version,
            "packet_schema_version": self.packet_schema_version,
            "research_packet_sha256": self.research_packet_sha256,
            "statement_category": self.statement_category,
        }


def _gold_name(value: object, field: str) -> str:
    """Validate one bounded, control-character-free proposed taxonomy name."""
    if not isinstance(value, str):
        raise ApprovalError(f"suggest_new gold case requires {field}")
    if any(unicodedata.category(character) == "Cc" for character in value):
        raise ApprovalError(f"gold case {field} must not contain control characters")
    collapsed = " ".join(value.split())
    if not collapsed:
        raise ApprovalError(f"gold case {field} must not be blank")
    if len(collapsed) > MAX_SUGGESTION_NAME_CHARS:
        raise ApprovalError(
            f"gold case {field} must be at most {MAX_SUGGESTION_NAME_CHARS} characters"
        )
    return collapsed


def _gold_id(value: object, field: str) -> int | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int):
        raise ApprovalError(f"gold case {field} must be an integer or null")
    return value


def _gold_evidence_urls(value: object, action: str) -> tuple[str, ...]:
    if value is None:
        raw: list[object] = []
    elif isinstance(value, list):
        raw = list(value)
    else:
        raise ApprovalError("gold case expected_evidence_urls must be a list or null")

    if action == ProviderAction.SUGGEST_NEW:
        if not MIN_GOLD_EVIDENCE_URLS <= len(raw) <= MAX_GOLD_EVIDENCE_URLS:
            raise ApprovalError("suggest_new gold case requires 1 to 3 evidence URLs")
    elif raw:
        raise ApprovalError(f"{action} gold case must not carry evidence URLs")

    urls: list[str] = []
    seen: set[str] = set()
    for item in raw:
        if not isinstance(item, str):
            raise ApprovalError("gold case evidence URL must be a string")
        parsed = urlparse(item)
        if parsed.scheme not in ("http", "https") or not parsed.netloc:
            raise ApprovalError("gold case evidence URL must be http or https")
        if item in seen:
            raise ApprovalError("gold case evidence URLs must be unique")
        seen.add(item)
        urls.append(item)
    return tuple(urls)


def parse_enriched_gold_cases(document: Any, database: str) -> list[EnrichedGoldCase]:
    """Read one database's packet-bound gold subset, strictly (V46/V49).

    Pure: this function reads no packet, calls no provider, and touches no
    database. Packet eligibility is checked separately by
    :func:`load_enriched_gold_cases`.
    """
    if database != ENRICHED_GOLD_DATABASE:
        raise ApprovalError(
            f"enriched gold cases exist only for the {ENRICHED_GOLD_DATABASE} "
            f"database; {database} stays unenriched"
        )
    if not isinstance(document, Mapping):
        raise ApprovalError("gold set must be a JSON object")
    raw_cases = document.get("cases")
    if not isinstance(raw_cases, list):
        raise ApprovalError("gold set must contain a 'cases' list")

    cases: list[EnrichedGoldCase] = []
    seen: set[str] = set()
    for raw in raw_cases:
        if not isinstance(raw, Mapping):
            raise ApprovalError("gold case must be an object")
        if raw.get("database") != database:
            continue
        if "research_packet_sha256" not in raw:
            raise ApprovalError(
                "finance gold case must bind a research packet; cases whose "
                "fingerprint predates packet identity are rejected and must be "
                "regenerated"
            )
        if set(raw) != ENRICHED_GOLD_CASE_FIELDS:
            raise ApprovalError("enriched gold case has invalid fields")

        action = raw.get("expected_action")
        if action not in ENRICHED_GOLD_ACTIONS:
            raise ApprovalError(
                "enriched gold case action must be select, suggest_new, or abstain"
            )

        fingerprint = raw.get("fingerprint")
        if not isinstance(fingerprint, str) or not fingerprint:
            raise ApprovalError("gold case requires a fingerprint")
        if fingerprint in seen:
            raise ApprovalError("gold case fingerprints must be unique per database")
        seen.add(fingerprint)

        merchant = raw.get("merchant")
        if not isinstance(merchant, str) or not merchant:
            raise ApprovalError("gold case requires a merchant")
        normalized_merchant = normalize_context_text(merchant)
        if not normalized_merchant:
            raise ApprovalError("gold case requires a non-blank merchant")

        amount = raw.get("amount_minor_units")
        if isinstance(amount, bool) or not isinstance(amount, int):
            raise ApprovalError("gold case requires integer amount_minor_units")

        statement_category = raw.get("statement_category")
        if statement_category is not None and not isinstance(statement_category, str):
            raise ApprovalError("gold case statement_category must be a string or null")

        try:
            packet_sha256 = require_packet_sha256(raw.get("research_packet_sha256"))
        except ValueError as exc:
            raise ApprovalError(str(exc)) from exc

        versions: dict[str, str] = {}
        for field in ("packet_schema_version", "packet_query_version"):
            version = raw.get(field)
            if not isinstance(version, str) or not version:
                raise ApprovalError(f"gold case requires {field}")
            versions[field] = version

        choice_id = _gold_id(raw.get("expected_choice_id"), "expected_choice_id")
        parent_id = _gold_id(
            raw.get("expected_parent_category_id"), "expected_parent_category_id"
        )
        evidence_urls = _gold_evidence_urls(
            raw.get("expected_evidence_urls"), str(action)
        )
        category_name: str | None = None
        subcategory_name: str | None = None

        if action == ProviderAction.SELECT:
            if choice_id is None:
                raise ApprovalError("select gold case requires an integer choice ID")
            for field in (
                "expected_category_name",
                "expected_subcategory_name",
                "expected_parent_category_id",
            ):
                if raw.get(field) is not None:
                    raise ApprovalError(f"select gold case must not carry {field}")
            parent_id = None
        elif action == ProviderAction.ABSTAIN:
            for field in (
                "expected_choice_id",
                "expected_category_name",
                "expected_subcategory_name",
                "expected_parent_category_id",
            ):
                if raw.get(field) is not None:
                    raise ApprovalError(f"abstain gold case must not carry {field}")
            choice_id = None
            parent_id = None
        else:
            if choice_id is not None:
                raise ApprovalError(
                    "suggest_new gold case must not carry expected_choice_id"
                )
            category_name = _gold_name(
                raw.get("expected_category_name"), "expected_category_name"
            )
            subcategory_name = _gold_name(
                raw.get("expected_subcategory_name"), "expected_subcategory_name"
            )

        cases.append(
            EnrichedGoldCase(
                database=database,
                fingerprint=fingerprint,
                merchant=normalized_merchant,
                amount_minor_units=amount,
                statement_category=normalize_optional_context_text(statement_category),
                research_packet_sha256=packet_sha256,
                packet_schema_version=versions["packet_schema_version"],
                packet_query_version=versions["packet_query_version"],
                expected_action=str(action),
                expected_choice_id=choice_id,
                expected_category_name=category_name,
                expected_subcategory_name=subcategory_name,
                expected_parent_category_id=parent_id,
                expected_evidence_urls=evidence_urls,
            )
        )

    return cases


def enriched_gold_subset_hash(cases: Iterable[EnrichedGoldCase]) -> str:
    """Hash one database's enriched gold subset in a stable order (V30)."""
    payload = sorted(
        (case.as_payload() for case in cases),
        key=lambda item: item["fingerprint"],
    )
    return sha256_hex(canonical_json_bytes(payload))


def enriched_packet_identity_hash(cases: Iterable[EnrichedGoldCase]) -> str:
    """Hash the exact packet identity every enriched case was approved against.

    A refresh, a version bump, or a re-bound context changes this digest, which
    invalidates approval before any LLM-driven mutation (V14/V30).
    """
    payload = sorted(
        (
            {
                "fingerprint": case.fingerprint,
                "packet_query_version": case.packet_query_version,
                "packet_schema_version": case.packet_schema_version,
                "research_packet_sha256": case.research_packet_sha256,
            }
            for case in cases
        ),
        key=lambda item: item["fingerprint"],
    )
    return sha256_hex(canonical_json_bytes(payload))


def approved_packet_for_enriched_case(
    case: EnrichedGoldCase, packet_resolver: Callable[[str], Any]
) -> Any:
    """Resolve the currently approved packet for one enriched gold case.

    The single eligibility gate shared by the gold loader and the validator, so
    the two can never drift. Raises before a categorizer exists when the packet
    is missing, rejected, stale, tampered, or unapproved, or when it no longer
    matches the case's bound hash, merchant, or schema/query versions
    (V47/V54).
    """
    label = case.fingerprint[:12]
    packet = getattr(packet_resolver(case.merchant), "packet", None)
    if packet is None:
        raise ApprovalError(
            f"enriched gold case {label} has no approved research packet"
        )
    if getattr(packet, "packet_sha256", None) != case.research_packet_sha256:
        raise ApprovalError(f"enriched gold case {label} binds a stale packet hash")
    if getattr(packet, "normalized_merchant", None) != case.merchant:
        raise ApprovalError(
            f"enriched gold case {label} does not match its packet merchant"
        )
    if case.packet_schema_version != RESEARCH_SCHEMA_VERSION or (
        getattr(packet, "schema_version", None) != RESEARCH_SCHEMA_VERSION
    ):
        raise ApprovalError(
            f"enriched gold case {label} packet schema version is stale"
        )
    if case.packet_query_version != RESEARCH_QUERY_VERSION or (
        getattr(packet, "query_version", None) != RESEARCH_QUERY_VERSION
    ):
        raise ApprovalError(f"enriched gold case {label} packet query version is stale")
    return packet


def load_enriched_gold_cases(
    database: str,
    choices: Iterable[Mapping[str, object]],
    *,
    packet_resolver: Callable[[str], Any],
    path: Path | None = None,
) -> list[EnrichedGoldCase]:
    """Load packet-bound gold cases whose packets are currently approved.

    Every case must resolve an approved packet with the exact bound hash, the
    same normalized merchant, the current schema and query versions, and a
    fingerprint that still matches its packet-bound context. Anything else
    raises before a categorizer is built, so a stale, tampered, rejected, or
    unapproved packet can never satisfy approval (V47/V54).
    """
    gold_path = path or private_gold_path()
    if not gold_path.exists():
        raise ApprovalError(
            f"private gold set {gold_path.name} is missing; write mode is not approved"
        )
    try:
        document = json.loads(gold_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ApprovalError("private gold set could not be read") from exc

    cases = parse_enriched_gold_cases(document, database)
    choice_rows = list(choices)
    for case in cases:
        approved_packet_for_enriched_case(case, packet_resolver)
        case.build_context(choice_rows)

    return cases


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


def build_enriched_fingerprints(
    database: str,
    base_url: str,
    model: str,
    choices: Iterable[Mapping[str, object]],
    cases: Iterable[EnrichedGoldCase],
) -> dict[str, Any]:
    """Build the exact identity an enriched approval record must match (V30)."""
    case_list = list(cases)
    return {
        "base_url": normalize_base_url(base_url),
        "database": database,
        "gold_sha256": enriched_gold_subset_hash(case_list),
        "mode": ENRICHED_AUTHORIZED_MODE,
        "model": model,
        "packet_identity_sha256": enriched_packet_identity_hash(case_list),
        "prompt_sha256": sha256_hex(canonical_enriched_prompt_bytes()),
        "protocol": ENRICHED_PROTOCOL,
        "schema_sha256": sha256_hex(canonical_enriched_response_schema_bytes()),
        "taxonomy_sha256": taxonomy_hash(database, choices),
    }


def load_enriched_approval_record(path: Path | None = None) -> Mapping[str, Any]:
    """Load the enriched approval record, never the unenriched one (V33)."""
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
    record = document.get(ENRICHED_APPROVAL_KEY)
    if not isinstance(record, Mapping):
        raise ApprovalError(
            f"approval record has no entry for {ENRICHED_APPROVAL_KEY}; the enriched "
            "gate needs its own approval and never reuses the unenriched one"
        )
    return record


def build_enriched_write_authorizer(cases: Iterable[EnrichedGoldCase]):
    """Authorize only an exact approved select for context + packet + choice."""
    approved = {
        case.fingerprint: case
        for case in cases
        if case.expected_action == ProviderAction.SELECT
    }

    def authorize(context: CanonicalContext, packet: Any, choice_id: int) -> bool:
        if getattr(packet, "packet_sha256", None) != context.research_packet_sha256:
            return False
        if (
            getattr(packet, "schema_version", None)
            != context.research_packet_schema_version
        ):
            return False
        if (
            getattr(packet, "query_version", None)
            != context.research_packet_query_version
        ):
            return False
        case = approved.get(context.fingerprint)
        if case is None:
            return False
        return case.expected_choice_id == choice_id

    return authorize


def authorize_enriched_write_mode(
    database: str,
    base_url: str,
    model: str,
    choices_provider: Callable[[], Iterable[Mapping[str, object]]],
    *,
    packet_resolver: Callable[[str], Any],
    gold_path: Path | None = None,
    approval_path: Path | None = None,
):
    """Return the enriched write authorizer, or raise before any DB mutation."""
    if database != ENRICHED_GOLD_DATABASE:
        raise ApprovalError(
            f"enriched write approval exists only for {ENRICHED_GOLD_DATABASE}"
        )
    choices = list(choices_provider())
    cases = load_enriched_gold_cases(
        database, choices, packet_resolver=packet_resolver, path=gold_path
    )
    if not cases:
        raise ApprovalError(
            f"private enriched gold set contains no cases for database {database}"
        )
    expected = build_enriched_fingerprints(database, base_url, model, choices, cases)
    record = load_enriched_approval_record(approval_path)
    verify_approval_record(record, expected)
    return build_enriched_write_authorizer(cases)

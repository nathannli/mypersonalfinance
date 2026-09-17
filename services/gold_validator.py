"""Three-pass gold validation for transaction-LLM write approval.

Validation runs through the exact production path: the same canonical
context builder, prompt/request builder, response schema, OpenCodex
client, response parser, and semantic validator used at runtime. Each
pass builds a fresh categorizer (empty cache, reset circuit) so a pass
cannot be satisfied by replaying an earlier pass's cache, and each case
must cost exactly one real provider request.

The validator performs no database writes and never sends the expected
result to the provider.
"""

import json
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

from config import Config
from services.categorizer_factory import build_categorizer
from services.llm_categorizer import OpenCodexCategorizer
from services.transaction_categorization import ProviderAction
from services.transaction_llm_approval import (
    REQUIRED_PASSES,
    ApprovalError,
    GoldCase,
    build_fingerprints,
)
from utils.repo_paths import private_approval_path

CategorizerFactory = Callable[[], OpenCodexCategorizer]


@dataclass(frozen=True)
class CaseResult:
    fingerprint: str
    matched: bool
    detail: str


@dataclass
class PassResult:
    results: list[CaseResult] = field(default_factory=list)
    provider_calls: int = 0
    failure: str | None = None

    @property
    def passed(self) -> bool:
        return self.failure is None and all(result.matched for result in self.results)

    @property
    def matched_count(self) -> int:
        return sum(1 for result in self.results if result.matched)


@dataclass
class ValidationResult:
    passes: list[PassResult] = field(default_factory=list)

    @property
    def approved(self) -> bool:
        return len(self.passes) == REQUIRED_PASSES and all(
            single.passed for single in self.passes
        )


def run_pass(
    cases: Sequence[GoldCase],
    choices: Iterable[Mapping[str, object]],
    categorizer_factory: CategorizerFactory,
) -> PassResult:
    """Run one fresh pass over one database's gold subset."""
    choice_rows = list(choices)
    categorizer = categorizer_factory()
    if categorizer.provider_call_count or categorizer.cache_hit_count:
        raise ApprovalError("validation pass requires a fresh categorizer")

    result = PassResult()
    for case in cases:
        context = case.build_context(choice_rows)
        calls_before = categorizer.provider_call_count
        cache_hits_before = categorizer.cache_hit_count

        categorization = categorizer.categorize(context)

        calls_made = categorizer.provider_call_count - calls_before
        cache_hits = categorizer.cache_hit_count - cache_hits_before
        if calls_made != 1 or cache_hits != 0:
            result.failure = (
                f"case {case.fingerprint[:12]} used {calls_made} provider call(s) "
                f"and {cache_hits} cache hit(s); exactly one fresh call is required"
            )
            return result
        if categorizer.circuit_open:
            result.failure = f"circuit opened during case {case.fingerprint[:12]}"
            return result

        choice_id = (
            categorization.choice_id
            if categorization.action == ProviderAction.SELECT
            else None
        )
        matched = case.matches(str(categorization.action), choice_id)
        result.results.append(
            CaseResult(
                fingerprint=case.fingerprint,
                matched=matched,
                detail=f"{categorization.action}:{choice_id}",
            )
        )
        result.provider_calls += calls_made

    return result


def run_validation(
    cases: Sequence[GoldCase],
    choices: Iterable[Mapping[str, object]],
    categorizer_factory: CategorizerFactory,
    required_passes: int = REQUIRED_PASSES,
) -> ValidationResult:
    """Run consecutive fresh passes, stopping at the first failing pass."""
    if not cases:
        raise ApprovalError("gold subset contains no cases for this database")

    choice_rows = list(choices)
    validation = ValidationResult()
    for _ in range(required_passes):
        single = run_pass(cases, choice_rows, categorizer_factory)
        validation.passes.append(single)
        if not single.passed:
            break
    return validation


def production_categorizer_factory(config: Config) -> CategorizerFactory:
    """Build fresh production categorizers, one per validation pass."""

    def factory() -> OpenCodexCategorizer:
        return build_categorizer(config)

    return factory


def write_approval_record(
    database: str,
    base_url: str,
    model: str,
    choices: Iterable[Mapping[str, object]],
    cases: Sequence[GoldCase],
    validation: ValidationResult,
    approval_path: Path | None = None,
) -> Path:
    """Persist approval for one database after three fresh passing runs."""
    if not validation.approved:
        raise ApprovalError(
            "approval requires three consecutive fully passing validation runs"
        )

    path = approval_path or private_approval_path()
    document: dict = {}
    if path.exists():
        try:
            existing = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise ApprovalError("existing approval record could not be read") from exc
        if isinstance(existing, Mapping):
            document = dict(existing)

    record = build_fingerprints(database, base_url, model, choices, cases)
    record["passes"] = len(validation.passes)
    record["approved_at"] = datetime.now(UTC).isoformat()
    document[database] = record

    path.write_text(
        json.dumps(document, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    return path

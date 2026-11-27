"""Three-pass gold validation for transaction-LLM write approval.

Validation runs through the exact production path: the same canonical
context builder, prompt/request builder, response schema, OpenCodex
client, response parser, and semantic validator used at runtime. Each
model-routed case requires exactly one fresh provider request. Enriched
validation can also check deterministic routes without a provider call.

The validator performs no database writes and never sends the expected
result to the provider.
"""

import contextlib
import json
import os
import tempfile
from collections.abc import Callable, Iterable, Mapping, Sequence
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

from config import Config
from services.categorizer_factory import build_categorizer
from services.deterministic_categorization import (
    DeterministicOutcome,
    DeterministicResolution,
)
from services.llm_categorizer import (
    EnrichedAbstain,
    EnrichedSelect,
    EnrichedSuggestion,
    OpenCodexCategorizer,
)
from services.transaction_categorization import ProviderAction, UnresolvedReason
from services.transaction_llm_approval import (
    ENRICHED_APPROVAL_KEY,
    REQUIRED_PASSES,
    ApprovalError,
    EnrichedGoldCase,
    GoldCase,
    approved_packet_for_enriched_case,
    build_enriched_fingerprints,
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


@dataclass(frozen=True)
class EnrichedCaseResult:
    fingerprint: str
    matched: bool
    detail: str

    deterministic: bool = False

@dataclass
class EnrichedPassResult:
    results: list[EnrichedCaseResult] = field(default_factory=list)
    provider_calls: int = 0
    failure: str | None = None

    @property
    def passed(self) -> bool:
        return self.failure is None and all(result.matched for result in self.results)

    @property
    def matched_count(self) -> int:
        return sum(1 for result in self.results if result.matched)


@dataclass
class EnrichedValidationResult:
    passes: list[EnrichedPassResult] = field(default_factory=list)

    @property
    def approved(self) -> bool:
        return len(self.passes) == REQUIRED_PASSES and all(
            single.passed for single in self.passes
        )


def describe_enriched_decision(decision: object) -> str:
    """Render a model decision for local reporting.

    Only the model's own output is rendered, never the gold expectation.
    """
    if isinstance(decision, EnrichedSelect):
        return f"{ProviderAction.SELECT}:{decision.choice_id}"
    if isinstance(decision, EnrichedSuggestion):
        return (
            f"{ProviderAction.SUGGEST_NEW}:"
            f"{decision.category_name}/{decision.subcategory_name}"
        )
    if isinstance(decision, EnrichedAbstain):
        return f"{ProviderAction.ABSTAIN}:None"
    return f"invalid:{type(decision).__name__}"


def evaluate_enriched_case(
    case: EnrichedGoldCase, decision: object
) -> EnrichedCaseResult:
    """Compare one enriched decision with one approved expectation.

    Pure: the caller performs the provider call. The enriched three-pass
    runner and its approval identity arrive with the runtime binding.
    """
    return EnrichedCaseResult(
        fingerprint=case.fingerprint,
        matched=case.matches(decision),
        detail=describe_enriched_decision(decision),
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
) -> ValidationResult:
    """Run consecutive fresh passes, stopping at the first failing pass."""
    if not cases:
        raise ApprovalError("gold subset contains no cases for this database")

    choice_rows = list(choices)
    validation = ValidationResult()
    for _ in range(REQUIRED_PASSES):
        single = run_pass(cases, choice_rows, categorizer_factory)
        validation.passes.append(single)
        if not single.passed:
            break
    return validation


def production_categorizer_factory(
    config: Config, *, model: str | None = None
) -> CategorizerFactory:
    """Build fresh production categorizers, one per validation pass.

    ``model`` must be the exact model the validated path actually calls, so an
    enriched validation binds the enriched model and a legacy validation the
    unenriched one; ``None`` keeps the unenriched default (V51).
    """

    def factory() -> OpenCodexCategorizer:
        return build_categorizer(config, model=model)

    return factory


def run_enriched_pass(
    cases: Sequence[EnrichedGoldCase],
    choices: Iterable[Mapping[str, object]],
    categorizer_factory: CategorizerFactory,
    *,
    packet_resolver: Callable[[str], object],
) -> EnrichedPassResult:
    on_case: Callable[[EnrichedCaseResult], None] | None = None,
    resolve_rule: Callable[[EnrichedGoldCase], DeterministicResolution] | None = None,
    """Run one fresh enriched pass through the exact production path (V31).

    Eligibility is re-checked per case immediately before its request, so a
    packet that was refreshed, tampered with, or unapproved between passes
    fails the pass instead of being answered from stale evidence.
    """
    choice_rows = list(choices)
    categorizer = categorizer_factory()
    if categorizer.provider_call_count or categorizer.cache_hit_count:
        raise ApprovalError("validation pass requires a fresh categorizer")

    result = EnrichedPassResult()
    for case in cases:
        packet = approved_packet_for_enriched_case(case, packet_resolver)
        context = case.build_context(choice_rows)
        calls_before = categorizer.provider_call_count
        if resolve_rule is not None:
            rule = resolve_rule(case)
            if rule.outcome != DeterministicOutcome.NO_MATCH:
                choice_id = rule.choice["subcategory_id"] if rule.choice else None
                checked = EnrichedCaseResult(
                    case.fingerprint,
                    choice_id is not None
                    and case.expected_action == ProviderAction.SELECT
                    and choice_id == case.expected_choice_id,
                    f"deterministic:{choice_id}",
                    deterministic=True,
                )
                result.results.append(checked)
                if on_case is not None:
                    on_case(checked)
                continue
        cache_hits_before = categorizer.cache_hit_count

        execution = categorizer.categorize_enriched(context, packet)

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
        if execution.decision is None:
            reason = execution.reason or UnresolvedReason.MALFORMED
            result.failure = f"case {case.fingerprint[:12]} failed: {reason}"
            return result

        result.results.append(evaluate_enriched_case(case, execution.decision))
        result.provider_calls += calls_made

        if on_case is not None:
            on_case(result.results[-1])

    return result

    return result
def _run_parallel_enriched_pass(
    cases: Sequence[EnrichedGoldCase],
    choices: list[Mapping[str, object]],
    categorizer_factory: CategorizerFactory,
    packet_resolver: Callable[[str], object],
    workers: int,
    on_case: Callable[[EnrichedCaseResult], None],
    resolve_rule: Callable[[EnrichedGoldCase], DeterministicResolution] | None,
) -> EnrichedPassResult:
    result = EnrichedPassResult()
    with ThreadPoolExecutor(max_workers=workers) as pool:
        for start in range(0, len(cases), workers):
            batch = [
                pool.submit(
                    run_enriched_pass, (case,), choices, categorizer_factory,
                    packet_resolver=packet_resolver,
                    resolve_rule=resolve_rule,
                )
                for case in cases[start:start + workers]
            ]
            for future in as_completed(batch):
                for case_result in future.result().results:
                    on_case(case_result)
            for future in batch:
                single = future.result()
                result.results.extend(single.results)
                result.provider_calls += single.provider_calls
                result.failure = result.failure or single.failure
            if result.failure is not None:
                break


def run_enriched_validation(
    cases: Sequence[EnrichedGoldCase],
    choices: Iterable[Mapping[str, object]],
    categorizer_factory: CategorizerFactory,
    *,
    packet_resolver: Callable[[str], object],
) -> EnrichedValidationResult:
    workers: int = 1,
    on_case: Callable[[int, EnrichedCaseResult], None] | None = None,
    resolve_rule: Callable[[EnrichedGoldCase], DeterministicResolution] | None = None,
    """Run consecutive fresh enriched passes, stopping at the first failure."""
    if not cases:
        raise ApprovalError("enriched gold subset contains no cases for this database")

    if not 1 <= workers <= 3:
        raise ValueError("workers must be between 1 and 3")
    choice_rows = list(choices)
    validation = EnrichedValidationResult()
    for pass_number in range(1, REQUIRED_PASSES + 1):
        def report(case_result: EnrichedCaseResult) -> None:
            if on_case is not None:
                on_case(pass_number, case_result)

        if workers == 1:
            single = run_enriched_pass(
                cases, choice_rows, categorizer_factory,
                packet_resolver=packet_resolver, on_case=report,
                resolve_rule=resolve_rule,
            )
        else:
            single = _run_parallel_enriched_pass(
                cases, choice_rows, categorizer_factory, packet_resolver, workers, report,
                resolve_rule,
            )
        validation.passes.append(single)
        if not single.passed:
            break
    return validation


def _atomic_write_text(path: Path, payload: str) -> None:
    """temp file + fsync + atomic replace, matching the research store (V50).

    This record authorizes writes, so a crash must leave the previous record
    intact rather than a truncated one. A truncated record already fails closed
    on read, but destroying a valid approval is not worth the risk.
    """

    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=str(path.parent)
    )
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(temporary)
        raise

    with contextlib.suppress(OSError):
        directory = os.open(str(path.parent), os.O_RDONLY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)


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

    _atomic_write_text(path, json.dumps(document, indent=2, sort_keys=True) + "\n")
    return path


def write_enriched_approval_record(
    database: str,
    base_url: str,
    model: str,
    choices: Iterable[Mapping[str, object]],
    cases: Sequence[EnrichedGoldCase],
    validation: EnrichedValidationResult,
    approval_path: Path | None = None,
) -> Path:
    """Persist enriched approval after three fresh passing runs (V33).

    Writes only the enriched key: the unenriched ``finance`` and
    ``parents_finance`` entries in the same document are preserved exactly.
    """
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

    record = build_enriched_fingerprints(database, base_url, model, choices, cases)
    record["passes"] = len(validation.passes)
    # A case may authorize model writes only if the model passed it every time.
    model_cases = {case.fingerprint for case in cases}
    for single in validation.passes:
        model_cases.intersection_update(
            result.fingerprint for result in single.results if not result.deterministic
        )
    if not model_cases:
        raise ApprovalError("no cases passed through the model in all three passes")
    record["model_case_fingerprints"] = sorted(model_cases)
    record["approved_at"] = datetime.now(UTC).isoformat()
    document[ENRICHED_APPROVAL_KEY] = record

    _atomic_write_text(path, json.dumps(document, indent=2, sort_keys=True) + "\n")
    return path

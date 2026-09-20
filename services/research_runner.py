"""Research runner: discovery, one research operation per merchant, run status.

This is the only module that turns merchant rows into frozen evidence. It is
the sole caller of TinyFish (V3) and writes nothing but packets and review
state: no expense, category, subcategory, deletion, or auto-match mutation,
and no OpenCodex request (V4).
"""

from __future__ import annotations

from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, replace
from enum import StrEnum
from pathlib import Path

from services.deterministic_categorization import (
    DeterministicOutcome,
    resolve_deterministic_choice,
)
from services.research_packets import (
    MAX_FETCH_URLS,
    FetchedPage,
    PacketReviewStatus,
    PacketStatus,
    ResearchPacket,
    ResearchRunStatus,
    SearchResult,
    load_packet_if_present,
    packet_id_for,
    review_record_for,
    store_failure_packet,
    store_packet,
    utc_now,
)
from services.research_query import (
    derive_query,
    matched_relevance_tokens,
    significant_tokens,
)
from services.tinyfish_research import (
    ResearchCircuitOpenError,
    ResearchEmptyEvidenceError,
    ResearchError,
    ResearchFetchFailedError,
    ResearchIrrelevantError,
    TinyFishClient,
)
from services.transaction_categorization import (
    UnresolvedReason,
    normalize_context_text,
)

# Sentinels used only in this module's reporting, never as packet contents.
STATE_REASONS: frozenset[UnresolvedReason] = frozenset(
    {
        UnresolvedReason.RESEARCH_MISSING,
        UnresolvedReason.RESEARCH_STALE,
        UnresolvedReason.RESEARCH_TAMPERED,
        UnresolvedReason.RESEARCH_UNAPPROVED,
    }
)


class TargetOperation(StrEnum):
    """What the run was actually asked to do for one merchant."""

    REUSE = "reuse"
    """A valid current packet already existed and no refresh was requested."""

    RESEARCH = "research"
    """Initial research, because no usable packet existed."""

    REFRESH = "refresh"
    """An explicit refresh of an existing packet."""


class TargetOutcome(StrEnum):
    SUCCEEDED = "succeeded"
    FAILED = "failed"


@dataclass(frozen=True)
class ResearchTarget:
    """One normalized merchant to research, deduplicated across input rows."""

    normalized_merchant: str
    raw_merchant: str

    @property
    def packet_id(self) -> str:
        return packet_id_for(self.normalized_merchant)


@dataclass(frozen=True)
class ResearchTargetResult:
    """The outcome of the operation requested for one target."""

    normalized_merchant: str
    operation: TargetOperation
    outcome: TargetOutcome
    packet_id: str
    packet_sha256: str | None = None
    status: PacketStatus | None = None
    review_status: PacketReviewStatus = PacketReviewStatus.PENDING
    failure_reason: UnresolvedReason | None = None
    preserved_previous_packet: bool = False

    @property
    def succeeded(self) -> bool:
        return self.outcome is TargetOutcome.SUCCEEDED

    @property
    def reused(self) -> bool:
        return self.succeeded and self.operation is TargetOperation.REUSE

    @property
    def is_refresh_failure(self) -> bool:
        """A failed refresh, which V60 keeps separate from live evidence."""

        return self.operation is TargetOperation.REFRESH and not self.succeeded


@dataclass(frozen=True)
class ResearchRunSummary:
    """Coverage counts and the exhaustive run status (V58, V59)."""

    status: ResearchRunStatus
    total: int
    succeeded: int
    failed: int
    reused: int
    researched: int
    refreshed: int
    refresh_failures: tuple[ResearchTargetResult, ...]
    failures: tuple[ResearchTargetResult, ...]

    @property
    def exit_code(self) -> int:
        return 1 if self.status is ResearchRunStatus.FAILED else 0


def discover_targets(
    rows: Iterable[Mapping[str, object]],
    *,
    card_type: str,
    choices: Sequence[Mapping[str, object]],
    auto_match: Callable[[str], tuple[str, str] | None],
) -> tuple[ResearchTarget, ...]:
    """The deduplicated merchants this run should research (V2, V4).

    A row is a target only when the deterministic resolver reports no mapping
    at all. ``MATCHED`` rows are already known, and ``INVALID_MAPPING`` rows are
    deliberately excluded: a mapping exists that the live taxonomy cannot
    honour, so researching the merchant would paper over a data bug that needs
    a human.
    """

    seen: dict[str, ResearchTarget] = {}
    for row in rows:
        merchant = row.get("merchant")
        if not isinstance(merchant, str) or not merchant.strip():
            continue
        resolution = resolve_deterministic_choice(
            card_type=card_type,
            cc_category=_optional_text(row.get("cc_category")),
            choices=choices,
            auto_match=lambda merchant=merchant: auto_match(merchant),
        )
        if resolution.outcome is not DeterministicOutcome.NO_MATCH:
            continue
        normalized = normalize_context_text(merchant)
        if not normalized or normalized in seen:
            continue
        seen[normalized] = ResearchTarget(normalized, merchant)
    return tuple(seen.values())


def review_status_for(
    packet_id: str, *, root: Path | None = None
) -> PacketReviewStatus:
    record = review_record_for(packet_id, root=root)
    return record.status if record is not None else PacketReviewStatus.PENDING


def run_target(
    target: ResearchTarget,
    *,
    client: TinyFishClient,
    refresh: bool = False,
    root: Path | None = None,
) -> ResearchTargetResult:
    """Perform the operation this run requested for one target.

    The operation, not the final packet, decides success: a failed refresh is a
    failure even though the previous approved packet survives it intact (V59,
    V60).
    """

    packet_id = target.packet_id
    existing = load_packet_if_present(packet_id, root=root)

    if not refresh:
        if existing is not None and existing.status is PacketStatus.COMPLETE:
            if existing.is_stale():
                # V57/V14: version drift blocks use until an explicit refresh.
                # Replacing it here would be a silent refresh, which V13 bans.
                return ResearchTargetResult(
                    normalized_merchant=target.normalized_merchant,
                    operation=TargetOperation.RESEARCH,
                    outcome=TargetOutcome.FAILED,
                    packet_id=packet_id,
                    packet_sha256=existing.packet_sha256,
                    status=existing.status,
                    review_status=review_status_for(packet_id, root=root),
                    failure_reason=UnresolvedReason.RESEARCH_STALE,
                )
            return _reused(target, existing, root=root)
        # Missing, or a typed failure packet with no usable evidence (V55).
        operation = TargetOperation.RESEARCH
    else:
        operation = TargetOperation.REFRESH

    try:
        packet = _research(target, client=client)
    except Exception as error:  # noqa: BLE001 - typed below
        reason = _reason_for(error)
    else:
        store_packet(packet, root=root)
        return ResearchTargetResult(
            normalized_merchant=target.normalized_merchant,
            operation=operation,
            outcome=TargetOutcome.SUCCEEDED,
            packet_id=packet_id,
            packet_sha256=packet.packet_sha256,
            status=PacketStatus.COMPLETE,
            review_status=PacketReviewStatus.PENDING,
        )

    # Failure. A failed refresh must not destroy valid frozen evidence, so the
    # failure packet is only persisted when no complete packet is active (V60).
    preserved = False
    if operation is TargetOperation.REFRESH:
        preserved = (
            store_failure_packet(_failure_packet(target, reason), root=root) is None
        )
    else:
        store_failure_packet(_failure_packet(target, reason), root=root)

    return ResearchTargetResult(
        normalized_merchant=target.normalized_merchant,
        operation=operation,
        outcome=TargetOutcome.FAILED,
        packet_id=packet_id,
        packet_sha256=existing.packet_sha256 if existing is not None else None,
        status=existing.status if existing is not None else PacketStatus.FAILED,
        review_status=review_status_for(packet_id, root=root),
        failure_reason=reason,
        preserved_previous_packet=preserved,
    )


def run_targets(
    targets: Sequence[ResearchTarget],
    *,
    client: TinyFishClient,
    refresh: bool = False,
    root: Path | None = None,
) -> tuple[ResearchTargetResult, ...]:
    return tuple(
        run_target(target, client=client, refresh=refresh, root=root)
        for target in targets
    )


def summarize(results: Sequence[ResearchTargetResult]) -> ResearchRunSummary:
    """Exhaustive run status over the requested operations (V59).

    ``complete`` for a valid run with zero targets or all operations
    succeeding; ``partial`` when at least one succeeds and at least one fails;
    ``failed`` when a nonempty target set yields zero successes. Startup,
    config, and input failures are decided by the caller, which reports
    ``failed`` without calling this.
    """

    succeeded = [result for result in results if result.succeeded]
    if not results:
        status = ResearchRunStatus.COMPLETE
    elif len(succeeded) == len(results):
        status = ResearchRunStatus.COMPLETE
    elif succeeded:
        status = ResearchRunStatus.PARTIAL
    else:
        status = ResearchRunStatus.FAILED

    return ResearchRunSummary(
        status=status,
        total=len(results),
        succeeded=len(succeeded),
        failed=len(results) - len(succeeded),
        reused=sum(1 for r in results if r.reused),
        researched=sum(
            1
            for r in results
            if r.succeeded and r.operation is TargetOperation.RESEARCH
        ),
        refreshed=sum(
            1 for r in results if r.succeeded and r.operation is TargetOperation.REFRESH
        ),
        refresh_failures=tuple(r for r in results if r.is_refresh_failure),
        failures=tuple(r for r in results if not r.succeeded),
    )


def _reused(
    target: ResearchTarget, existing: ResearchPacket, *, root: Path | None
) -> ResearchTargetResult:
    return ResearchTargetResult(
        normalized_merchant=target.normalized_merchant,
        operation=TargetOperation.REUSE,
        outcome=TargetOutcome.SUCCEEDED,
        packet_id=existing.packet_id,
        packet_sha256=existing.packet_sha256,
        status=existing.status,
        review_status=review_status_for(existing.packet_id, root=root),
    )


def _research(target: ResearchTarget, *, client: TinyFishClient) -> ResearchPacket:
    """One logical Search plus up to three independent Fetch requests (V26).

    Failure precedence follows V29, in order: zero significant tokens returns
    ``research_irrelevant`` with zero requests, then the client's own typed
    Search failures, then all-fetches-failed with one shared reason, then mixed
    fetch failures, then zero bounded characters as ``research_empty_evidence``,
    then relevance.
    """

    derived_query = derive_query(target.normalized_merchant)
    tokens = significant_tokens(derived_query)
    if not tokens:
        # V44/V53: decided before Search, so this issues zero requests.
        raise ResearchIrrelevantError("derived query has no significant tokens")

    results = client.search(derived_query)
    pages, errors, fetched_with_text = _fetch_evidence(results, tokens, client=client)

    if not pages:
        raise _evidence_failure(errors, fetched_with_text)

    return ResearchPacket(
        normalized_merchant=target.normalized_merchant,
        derived_query=derived_query,
        status=PacketStatus.COMPLETE,
        searched_at=utc_now(),
        search_results=results,
        fetched_pages=pages,
    )


def _fetch_evidence(
    results: Sequence[SearchResult], tokens: tuple[str, ...], *, client: TinyFishClient
) -> tuple[tuple[FetchedPage, ...], list[ResearchError], bool]:
    """Fetch each retained URL independently, keeping only relevant pages.

    Each Fetch is its own request, so one URL cannot consume another's budget
    (V42). A URL that fails is recorded and the remaining URLs still run.
    """

    pages: list[FetchedPage] = []
    errors: list[ResearchError] = []
    fetched_with_text = False

    for result in results[:MAX_FETCH_URLS]:
        try:
            page = client.fetch(result.url)
        except ResearchError as error:
            errors.append(error)
            continue
        fetched_with_text = True
        matched = matched_relevance_tokens(
            tokens, (page.title, page.description, page.text)
        )
        if matched:
            # V45: the token match is only a junk prefilter, never proof of
            # merchant identity.
            pages.append(replace(page, relevance_matched_tokens=matched))

    return tuple(pages), errors, fetched_with_text


def _evidence_failure(
    errors: Sequence[ResearchError], fetched_with_text: bool
) -> ResearchError:
    if fetched_with_text or not errors:
        # Bounded text was retrieved, but no page discussed the merchant.
        return ResearchIrrelevantError("no fetched page was relevant to the merchant")
    # V29 treats a fetch that yields zero bounded characters as a *successful*
    # fetch, not a failing one, so "all fetches failed" holds only while every
    # recorded error is an actual Fetch failure.
    failures = [
        error for error in errors if not isinstance(error, ResearchEmptyEvidenceError)
    ]
    if len(failures) != len(errors):
        # V29 clause 6: at least one fetch succeeded but returned nothing, so
        # the fetches did not all fail, however the others ended.
        return ResearchEmptyEvidenceError("no fetched page yielded bounded evidence")
    kinds = {type(error) for error in failures}
    if len(kinds) == 1:
        # V29: one shared terminal reason is preserved as-is.
        return failures[0]
    return ResearchFetchFailedError("every fetched URL failed differently")


def _failure_packet(target: ResearchTarget, reason: UnresolvedReason) -> ResearchPacket:
    """A failed packet records the derived term the run actually used (V44).

    Derivation is pure, so it is recomputed here instead of threaded through
    the failure path. A descriptor that derives nothing usable has no search
    term to record, so the field stays empty rather than echoing the raw
    descriptor, which V44 forbids as a query.
    """

    try:
        derived_query = derive_query(target.normalized_merchant)
    except ResearchIrrelevantError:
        derived_query = ""

    return ResearchPacket(
        normalized_merchant=target.normalized_merchant,
        derived_query=derived_query,
        status=PacketStatus.FAILED,
        searched_at=utc_now(),
        failure_reason=reason,
    )


def _reason_for(error: BaseException) -> UnresolvedReason:
    """The typed reason for a per-target failure.

    Only research-execution reasons may reach a packet (V55), so a state reason
    or an unknown failure degrades to ``research_provider_error`` rather than
    producing a packet that cannot be stored.
    """

    if isinstance(error, ResearchCircuitOpenError):
        reason: UnresolvedReason | None = error.reason
    else:
        reason = getattr(type(error), "reason", None)
    if not isinstance(reason, UnresolvedReason) or reason in STATE_REASONS:
        return UnresolvedReason.RESEARCH_PROVIDER_ERROR
    return reason


def _optional_text(value: object) -> str | None:
    if isinstance(value, str) and value.strip():
        return value
    return None

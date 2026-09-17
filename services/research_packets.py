"""Canonical TinyFish research-packet, review, and category-suggestion types.

Types, canonical serialization/hashing, bounded-field validation, and the
private packet/review stores (V11-V13, V24, V50, V52, V55, V57, V60).
"""

from __future__ import annotations

import contextlib
import fcntl
import hashlib
import json
import os
import re
import tempfile
import unicodedata
from contextlib import contextmanager
from dataclasses import dataclass, replace
from datetime import datetime, timedelta, timezone
from enum import StrEnum
from pathlib import Path
from typing import Any, Iterator, Mapping
from urllib.parse import urlparse

from services.transaction_categorization import (
    UnresolvedReason,
    normalize_context_text,
)
from utils.repo_paths import (
    PRIVATE_RESEARCH_DIRNAME,
    RESEARCH_LOCK_FILENAME,
    RESEARCH_REVIEW_FILENAME,
    SUGGESTION_FILENAME,
    repo_root,
)

RESEARCH_SCHEMA_VERSION = "transaction-web-research-v1"
RESEARCH_QUERY_VERSION = "merchant-research-v2"
RESEARCH_PACKET_ID_PREFIX = "mypersonalfinance-research-packet-v1:"
SUGGESTION_ID_PREFIX = "mypersonalfinance-category-suggestion-v1:"
SUGGESTION_ID_LENGTH = 16
SUGGESTION_SCHEMA_VERSION = "transaction-category-suggestions-v1"

MAX_SEARCH_RESULTS = 5
MAX_FETCH_URLS = 3
MAX_FETCH_CHARS_PER_PAGE = 4000
MAX_FETCH_CHARS_TOTAL = 12000

MAX_SUGGESTION_NAME_CHARS = 80
MAX_SUGGESTION_RATIONALE_CHARS = 500
MIN_EVIDENCE_URLS = 1
MAX_EVIDENCE_URLS = 3

MAX_REJECTION_REASON_CHARS = 500

DEFAULT_RESEARCH_PURPOSE = (
    "Identify this merchant and its primary goods or services for "
    "personal-finance categorization"
)
FIXED_RESEARCH_LOCATION = "CA"
FIXED_RESEARCH_LANGUAGE = "en"


class PacketStatus(StrEnum):
    COMPLETE = "complete"
    FAILED = "failed"


class PacketReviewStatus(StrEnum):
    PENDING = "pending"
    APPROVED = "approved"
    REJECTED = "rejected"


class ResearchRunStatus(StrEnum):
    COMPLETE = "complete"
    PARTIAL = "partial"
    FAILED = "failed"


# A failed packet may only carry the reason of the stage that actually failed
# while researching, never a load-time or review-time classification (V55).
RESEARCH_EXECUTION_REASONS: frozenset[UnresolvedReason] = frozenset(
    {
        UnresolvedReason.RESEARCH_AUTH,
        UnresolvedReason.RESEARCH_RATE_LIMIT,
        UnresolvedReason.RESEARCH_TIMEOUT,
        UnresolvedReason.RESEARCH_PROVIDER_ERROR,
        UnresolvedReason.RESEARCH_NO_RESULTS,
        UnresolvedReason.RESEARCH_NO_VALID_URLS,
        UnresolvedReason.RESEARCH_FETCH_FAILED,
        UnresolvedReason.RESEARCH_EMPTY_EVIDENCE,
        UnresolvedReason.RESEARCH_IRRELEVANT,
        UnresolvedReason.RESEARCH_MALFORMED,
    }
)

RESEARCH_STATE_REASONS: frozenset[UnresolvedReason] = frozenset(
    {
        UnresolvedReason.RESEARCH_MISSING,
        UnresolvedReason.RESEARCH_STALE,
        UnresolvedReason.RESEARCH_TAMPERED,
        UnresolvedReason.RESEARCH_UNAPPROVED,
    }
)


class ResearchPacketError(ValueError):
    """Base class for research-packet problems."""


class ResearchPacketMalformedError(ResearchPacketError):
    """Packet content is not well-formed."""


class ResearchPacketTamperedError(ResearchPacketError):
    """Stored bytes do not match their own content hash (V57)."""


def _canonical_bytes(payload: Mapping[str, Any]) -> bytes:
    return json.dumps(
        payload, ensure_ascii=False, separators=(",", ":"), sort_keys=True
    ).encode("utf-8")


def _sha256_hex(material: str) -> str:
    return hashlib.sha256(material.encode("utf-8")).hexdigest()


def packet_id_for(normalized_merchant: str) -> str:
    """Stable packet identity for a normalized merchant (V12)."""

    return _sha256_hex(f"{RESEARCH_PACKET_ID_PREFIX}{normalized_merchant}")


def utc_now() -> str:
    """The canonical UTC timestamp used for every research artifact."""

    return datetime.now(timezone.utc).isoformat()


def _reject_control_characters(value: str, field: str) -> None:
    if any(unicodedata.category(char) == "Cc" for char in value):
        raise ResearchPacketError(f"{field} must not contain control characters")


def _bounded_text(value: object, field: str, limit: int) -> str:
    if not isinstance(value, str):
        raise ResearchPacketError(f"{field} must be a string")
    _reject_control_characters(value, field)
    normalized = " ".join(value.split()).strip()
    if not normalized:
        raise ResearchPacketError(f"{field} must not be blank")
    if len(normalized) > limit:
        raise ResearchPacketError(f"{field} must be at most {limit} characters")
    return normalized


def _require_http_url(value: object, field: str) -> str:
    if not isinstance(value, str):
        raise ResearchPacketError(f"{field} must be a string")
    parsed = urlparse(value)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise ResearchPacketError(f"{field} must be an absolute http/https URL")
    return value


def _require_utc(value: object, field: str) -> str:
    if not isinstance(value, str) or not value:
        raise ResearchPacketError(f"{field} must be a non-empty string")
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as exc:
        raise ResearchPacketError(f"{field} must be an ISO-8601 timestamp") from exc
    if parsed.tzinfo is None or parsed.utcoffset() != timedelta(0):
        raise ResearchPacketError(f"{field} must be a UTC timestamp")
    return value


def _require_sha256_hex(value: object, field: str) -> str:
    if not isinstance(value, str) or len(value) != 64:
        raise ResearchPacketError(f"{field} must be a sha256 hex digest")
    return value


def _canonical_fingerprints(value: object) -> tuple[str, ...]:
    """Sorted unique canonical-context digests (V24)."""

    if not isinstance(value, (list, tuple)):
        raise ResearchPacketError("context_fingerprints must be a sequence")
    return tuple(
        sorted({_require_sha256_hex(item, "context_fingerprints") for item in value})
    )


def _require_choice_id(value: object) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ResearchPacketError("parent_category_id must be an integer")
    return value


def _exact_keys(data: Mapping[str, Any], expected: frozenset[str], field: str) -> None:
    if set(data) != expected:
        raise ResearchPacketMalformedError(f"{field} has invalid fields")


@dataclass(frozen=True)
class SearchResult:
    """One retained Search result, in rank order (V9, V10)."""

    position: int
    site_name: str
    title: str
    snippet: str
    url: str

    RESULT_FIELDS = frozenset({"position", "site_name", "title", "snippet", "url"})

    def as_dict(self) -> dict[str, Any]:
        return {
            "position": self.position,
            "site_name": self.site_name,
            "snippet": self.snippet,
            "title": self.title,
            "url": self.url,
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> SearchResult:
        _exact_keys(data, cls.RESULT_FIELDS, "search result")
        position = data["position"]
        if isinstance(position, bool) or not isinstance(position, int):
            raise ResearchPacketMalformedError("position must be an integer")
        return cls(
            position=position,
            site_name=str(data["site_name"]),
            title=str(data["title"]),
            snippet=str(data["snippet"]),
            url=_require_http_url(data["url"], "url"),
        )


@dataclass(frozen=True)
class FetchedPage:
    """One fetched page with its bounded, relevance-filtered evidence (V9, V10)."""

    url: str
    final_url: str
    title: str
    description: str
    text: str
    relevance_matched_tokens: tuple[str, ...] = ()

    PAGE_FIELDS = frozenset(
        {
            "url",
            "final_url",
            "title",
            "description",
            "text",
            "content_sha256",
            "relevance_matched_tokens",
        }
    )

    @property
    def content_sha256(self) -> str:
        return hashlib.sha256(self.text.encode("utf-8")).hexdigest()

    def as_dict(self) -> dict[str, Any]:
        return {
            "url": self.url,
            "final_url": self.final_url,
            "title": self.title,
            "description": self.description,
            "text": self.text,
            "content_sha256": self.content_sha256,
            "relevance_matched_tokens": list(self.relevance_matched_tokens),
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> FetchedPage:
        _exact_keys(data, cls.PAGE_FIELDS, "fetched page")
        tokens = data["relevance_matched_tokens"]
        if not isinstance(tokens, (list, tuple)):
            raise ResearchPacketMalformedError(
                "relevance_matched_tokens must be a list"
            )
        page = cls(
            url=_require_http_url(data["url"], "url"),
            final_url=_require_http_url(data["final_url"], "final_url"),
            title=str(data["title"]),
            description=str(data["description"]),
            text=str(data["text"]),
            relevance_matched_tokens=tuple(str(token) for token in tokens),
        )
        if page.content_sha256 != data["content_sha256"]:
            raise ResearchPacketTamperedError("page content hash does not match")
        return page


@dataclass(frozen=True)
class ResearchPacket:
    """Canonical frozen evidence for one normalized merchant (V9, V12, V55)."""

    normalized_merchant: str
    derived_query: str
    status: PacketStatus
    searched_at: str
    schema_version: str = RESEARCH_SCHEMA_VERSION
    query_version: str = RESEARCH_QUERY_VERSION
    purpose: str = DEFAULT_RESEARCH_PURPOSE
    location: str = FIXED_RESEARCH_LOCATION
    language: str = FIXED_RESEARCH_LANGUAGE
    search_results: tuple[SearchResult, ...] = ()
    fetched_pages: tuple[FetchedPage, ...] = ()
    failure_reason: UnresolvedReason | None = None

    PACKET_FIELDS = frozenset(
        {
            "schema_version",
            "query_version",
            "packet_id",
            "status",
            "failure_reason",
            "normalized_merchant",
            "derived_query",
            "purpose",
            "location",
            "language",
            "searched_at",
            "search_results",
            "fetched_pages",
            "packet_sha256",
        }
    )

    def __post_init__(self) -> None:
        self.validate()

    @property
    def packet_id(self) -> str:
        """Derived identity, so it can never drift from the merchant (V12)."""

        return packet_id_for(self.normalized_merchant)

    def as_payload(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "query_version": self.query_version,
            "packet_id": self.packet_id,
            "status": str(self.status),
            "failure_reason": (
                str(self.failure_reason) if self.failure_reason is not None else None
            ),
            "normalized_merchant": self.normalized_merchant,
            "derived_query": self.derived_query,
            "purpose": self.purpose,
            "location": self.location,
            "language": self.language,
            "searched_at": self.searched_at,
            "search_results": [result.as_dict() for result in self.search_results],
            "fetched_pages": [page.as_dict() for page in self.fetched_pages],
        }

    def to_bytes(self) -> bytes:
        return _canonical_bytes(self.as_payload())

    @property
    def packet_sha256(self) -> str:
        return hashlib.sha256(self.to_bytes()).hexdigest()

    def as_dict(self) -> dict[str, Any]:
        payload = self.as_payload()
        payload["packet_sha256"] = self.packet_sha256
        return payload

    def validate(self) -> None:
        if self.normalized_merchant != normalize_context_text(self.normalized_merchant):
            raise ResearchPacketError("normalized_merchant must be normalized")
        if not self.normalized_merchant:
            raise ResearchPacketError("normalized_merchant must not be blank")

        if not isinstance(self.status, PacketStatus):
            raise ResearchPacketError("status must be a PacketStatus")

        if self.purpose != DEFAULT_RESEARCH_PURPOSE:
            raise ResearchPacketError("purpose must be the fixed research purpose")
        if self.location != FIXED_RESEARCH_LOCATION:
            raise ResearchPacketError("location must be CA")
        if self.language != FIXED_RESEARCH_LANGUAGE:
            raise ResearchPacketError("language must be en")

        _require_utc(self.searched_at, "searched_at")
        _require_sha256_hex(self.packet_sha256, "packet_sha256")

        if self.status is PacketStatus.FAILED:
            if self.failure_reason not in RESEARCH_EXECUTION_REASONS:
                raise ResearchPacketError(
                    "failed packet requires a research-execution reason"
                )
            if self.search_results or self.fetched_pages:
                raise ResearchPacketError("failed packet must carry no evidence")
        else:
            if self.failure_reason is not None:
                raise ResearchPacketError("complete packet must have no failure reason")
            if not self.derived_query:
                raise ResearchPacketError("complete packet requires a derived query")
            if not self.fetched_pages:
                raise ResearchPacketError("complete packet requires fetched evidence")

        if len(self.search_results) > MAX_SEARCH_RESULTS:
            raise ResearchPacketError(
                f"at most {MAX_SEARCH_RESULTS} search results are retained"
            )
        if len(self.fetched_pages) > MAX_FETCH_URLS:
            raise ResearchPacketError(f"at most {MAX_FETCH_URLS} pages are fetched")

        result_urls = [result.url for result in self.search_results]
        if len(set(result_urls)) != len(result_urls):
            raise ResearchPacketError("search result URLs must be unique")

        page_urls = [page.url for page in self.fetched_pages]
        if len(set(page_urls)) != len(page_urls):
            raise ResearchPacketError("fetched page URLs must be unique")
        if not set(page_urls).issubset(set(result_urls)):
            raise ResearchPacketError(
                "fetched pages must come from retained search results"
            )
        for position, result in enumerate(self.search_results):
            if result.position != position:
                raise ResearchPacketError("search results must be ranked in order")

        total_characters = 0
        for page in self.fetched_pages:
            if len(page.text) > MAX_FETCH_CHARS_PER_PAGE:
                raise ResearchPacketError(
                    f"page text must be at most {MAX_FETCH_CHARS_PER_PAGE} characters"
                )
            total_characters += len(page.text)
        if total_characters > MAX_FETCH_CHARS_TOTAL:
            raise ResearchPacketError(
                f"fetched evidence must be at most {MAX_FETCH_CHARS_TOTAL} characters"
            )

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> ResearchPacket:
        if not isinstance(data, Mapping):
            raise ResearchPacketMalformedError("packet must be a mapping")
        _exact_keys(data, cls.PACKET_FIELDS, "packet")

        stored_hash = _require_sha256_hex(data["packet_sha256"], "packet_sha256")

        try:
            status = PacketStatus(str(data["status"]))
        except ValueError as exc:
            raise ResearchPacketMalformedError("packet status is unknown") from exc

        raw_reason = data["failure_reason"]
        if raw_reason is None:
            failure_reason = None
        else:
            try:
                failure_reason = UnresolvedReason(str(raw_reason))
            except ValueError as exc:
                raise ResearchPacketMalformedError(
                    "packet failure_reason is unknown"
                ) from exc

        results = data["search_results"]
        pages = data["fetched_pages"]
        if not isinstance(results, (list, tuple)) or not isinstance(
            pages, (list, tuple)
        ):
            raise ResearchPacketMalformedError(
                "search_results and fetched_pages must be lists"
            )

        packet = cls(
            normalized_merchant=str(data["normalized_merchant"]),
            derived_query=str(data["derived_query"]),
            status=status,
            searched_at=str(data["searched_at"]),
            schema_version=str(data["schema_version"]),
            query_version=str(data["query_version"]),
            purpose=str(data["purpose"]),
            location=str(data["location"]),
            language=str(data["language"]),
            search_results=tuple(SearchResult.from_dict(row) for row in results),
            fetched_pages=tuple(FetchedPage.from_dict(row) for row in pages),
            failure_reason=failure_reason,
        )

        if str(data["packet_id"]) != packet.packet_id:
            raise ResearchPacketMalformedError("packet_id does not match merchant")

        if packet.packet_sha256 != stored_hash:
            raise ResearchPacketTamperedError("packet content hash does not match")

        return packet

    def is_stale(self) -> bool:
        """Version drift is stale; a hash mismatch is tampering (V57)."""

        return (
            self.schema_version != RESEARCH_SCHEMA_VERSION
            or self.query_version != RESEARCH_QUERY_VERSION
        )


@dataclass(frozen=True)
class PacketReviewRecord:
    """Human decision bound to one exact packet hash (V47, V52)."""

    packet_id: str
    packet_sha256: str
    status: PacketReviewStatus
    reviewed_at: str
    schema_version: str = RESEARCH_SCHEMA_VERSION
    query_version: str = RESEARCH_QUERY_VERSION
    reason: str | None = None

    RECORD_FIELDS = frozenset(
        {
            "packet_id",
            "packet_sha256",
            "status",
            "reason",
            "reviewed_at",
            "schema_version",
            "query_version",
        }
    )

    def __post_init__(self) -> None:
        if not isinstance(self.status, PacketReviewStatus):
            raise ResearchPacketError("status must be a PacketReviewStatus")
        if self.status is PacketReviewStatus.PENDING:
            raise ResearchPacketError("a stored review record must be decided")
        _require_sha256_hex(self.packet_sha256, "packet_sha256")
        _require_utc(self.reviewed_at, "reviewed_at")

        if self.status is PacketReviewStatus.REJECTED:
            object.__setattr__(
                self,
                "reason",
                _bounded_text(self.reason, "reason", MAX_REJECTION_REASON_CHARS),
            )
        elif self.reason is not None:
            raise ResearchPacketError("only a rejected record carries a reason")

    def as_dict(self) -> dict[str, Any]:
        return {
            "packet_id": self.packet_id,
            "packet_sha256": self.packet_sha256,
            "status": str(self.status),
            "reason": self.reason,
            "reviewed_at": self.reviewed_at,
            "schema_version": self.schema_version,
            "query_version": self.query_version,
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> PacketReviewRecord:
        _exact_keys(data, cls.RECORD_FIELDS, "review record")
        try:
            status = PacketReviewStatus(str(data["status"]))
        except ValueError as exc:
            raise ResearchPacketMalformedError("review status is unknown") from exc
        raw_reason = data["reason"]
        return cls(
            packet_id=str(data["packet_id"]),
            packet_sha256=str(data["packet_sha256"]),
            status=status,
            reason=None if raw_reason is None else str(raw_reason),
            reviewed_at=str(data["reviewed_at"]),
            schema_version=str(data["schema_version"]),
            query_version=str(data["query_version"]),
        )


@dataclass(frozen=True)
class CategorySuggestion:
    """Review-only taxonomy proposal; never mutates taxonomy (V19, V21)."""

    normalized_merchant: str
    category_name: str
    subcategory_name: str
    rationale: str
    evidence_urls: tuple[str, ...]
    research_packet_sha256: str
    parent_category_id: int | None = None
    context_fingerprints: tuple[str, ...] = ()

    SUGGESTION_FIELDS = frozenset(
        {
            "suggestion_id",
            "normalized_merchant",
            "category_name",
            "subcategory_name",
            "parent_category_id",
            "rationale",
            "evidence_urls",
            "research_packet_sha256",
            "context_fingerprints",
        }
    )

    def __post_init__(self) -> None:
        if self.normalized_merchant != normalize_context_text(self.normalized_merchant):
            raise ResearchPacketError("normalized_merchant must be normalized")
        if not self.normalized_merchant:
            raise ResearchPacketError("normalized_merchant must not be blank")

        object.__setattr__(
            self,
            "category_name",
            _bounded_text(
                self.category_name, "category_name", MAX_SUGGESTION_NAME_CHARS
            ),
        )
        object.__setattr__(
            self,
            "subcategory_name",
            _bounded_text(
                self.subcategory_name, "subcategory_name", MAX_SUGGESTION_NAME_CHARS
            ),
        )
        object.__setattr__(
            self,
            "rationale",
            _bounded_text(self.rationale, "rationale", MAX_SUGGESTION_RATIONALE_CHARS),
        )

        urls = tuple(
            _require_http_url(url, "evidence_urls") for url in self.evidence_urls
        )
        if not MIN_EVIDENCE_URLS <= len(urls) <= MAX_EVIDENCE_URLS:
            raise ResearchPacketError(
                f"evidence_urls must hold {MIN_EVIDENCE_URLS}-{MAX_EVIDENCE_URLS} URLs"
            )
        if len(set(urls)) != len(urls):
            raise ResearchPacketError("evidence_urls must be unique")
        object.__setattr__(self, "evidence_urls", urls)

        _require_sha256_hex(self.research_packet_sha256, "research_packet_sha256")
        if self.parent_category_id is not None:
            _require_choice_id(self.parent_category_id)

        object.__setattr__(
            self,
            "context_fingerprints",
            _canonical_fingerprints(self.context_fingerprints),
        )

    @property
    def suggestion_id(self) -> str:
        """Deduplicated by merchant, proposed taxonomy, and packet hash (V24).

        The proposed taxonomy includes ``parent_category_id``, so two proposals
        that differ only in their parent stay distinct.
        """

        material = "\x1f".join(
            [
                SUGGESTION_ID_PREFIX,
                self.normalized_merchant,
                normalize_context_text(self.category_name),
                normalize_context_text(self.subcategory_name),
                "" if self.parent_category_id is None else str(self.parent_category_id),
                self.research_packet_sha256,
            ]
        )
        return _sha256_hex(material)[:SUGGESTION_ID_LENGTH]

    def as_dict(self) -> dict[str, Any]:
        return {
            "suggestion_id": self.suggestion_id,
            "normalized_merchant": self.normalized_merchant,
            "category_name": self.category_name,
            "subcategory_name": self.subcategory_name,
            "parent_category_id": self.parent_category_id,
            "rationale": self.rationale,
            "evidence_urls": list(self.evidence_urls),
            "research_packet_sha256": self.research_packet_sha256,
            "context_fingerprints": list(self.context_fingerprints),
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> CategorySuggestion:
        _exact_keys(data, cls.SUGGESTION_FIELDS, "suggestion")
        urls = data["evidence_urls"]
        fingerprints = data["context_fingerprints"]
        if not isinstance(urls, (list, tuple)) or not isinstance(
            fingerprints, (list, tuple)
        ):
            raise ResearchPacketMalformedError(
                "evidence_urls and context_fingerprints must be lists"
            )
        raw_parent = data["parent_category_id"]
        suggestion = cls(
            normalized_merchant=str(data["normalized_merchant"]),
            category_name=str(data["category_name"]),
            subcategory_name=str(data["subcategory_name"]),
            rationale=str(data["rationale"]),
            evidence_urls=tuple(str(url) for url in urls),
            research_packet_sha256=str(data["research_packet_sha256"]),
            parent_category_id=(
                None if raw_parent is None else _require_choice_id(raw_parent)
            ),
            context_fingerprints=tuple(str(item) for item in fingerprints),
        )
        if str(data["suggestion_id"]) != suggestion.suggestion_id:
            raise ResearchPacketMalformedError("suggestion_id does not match content")
        return suggestion


# --------------------------------------------------------------------------- #
# Private stores (T4)
# --------------------------------------------------------------------------- #

_PACKET_ID_PATTERN = re.compile(r"\A[0-9a-f]{64}\Z")


class ResearchStoreError(ResearchPacketError):
    """Private store failure (bad path, unreadable or locked artifact)."""


class ResearchPacketMissingError(ResearchStoreError):
    """No frozen packet exists for this merchant, so the row is unresolved (V15)."""


class ResearchStoreLockedError(ResearchStoreError):
    """Another research writer holds the repository-root lock (V50)."""


def _store_root(root: Path | None) -> Path:
    # V11: every private path derives from the repository root, never the CWD.
    return Path(root) if root is not None else repo_root()


def _require_packet_id(value: object) -> str:
    if not isinstance(value, str) or not _PACKET_ID_PATTERN.match(value):
        # V11: the filename is the packet id and nothing else, so a non-digest
        # cannot introduce a path separator or traverse out of the directory.
        raise ResearchStoreError("packet_id must be a sha256 hex digest")
    return value


def packet_path_for(packet_id: str, *, root: Path | None = None) -> Path:
    """Path of one packet file, keyed by packet id alone (V11)."""

    return (
        _store_root(root)
        / PRIVATE_RESEARCH_DIRNAME
        / f"{_require_packet_id(packet_id)}.json"
    )


def review_records_path(*, root: Path | None = None) -> Path:
    """Path of the ignored research review artifact (V11, V52)."""

    return _store_root(root) / RESEARCH_REVIEW_FILENAME


@contextmanager
def research_writer_lock(*, root: Path | None = None) -> Iterator[None]:
    """One repository-root exclusive lock around every research write (V50).

    An advisory ``flock`` is used so a crashed writer cannot leave a stale lock
    behind: the kernel releases it when the descriptor closes. A second writer
    fails fast instead of mutating anything.
    """

    path = _store_root(root) / RESEARCH_LOCK_FILENAME
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor = os.open(path, os.O_CREAT | os.O_RDWR, 0o600)
    try:
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            raise ResearchStoreLockedError(
                "another research writer holds the repository-root lock"
            ) from exc
        yield
    finally:
        # Closing the descriptor releases the lock, so every write below has
        # completed (temp file + fsync + atomic replace) before this point.
        os.close(descriptor)


def _atomic_write(path: Path, payload: bytes) -> None:
    """temp file + fsync + atomic replace, so no reader sees a partial file."""

    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=str(path.parent)
    )
    try:
        with os.fdopen(descriptor, "wb") as handle:
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


def load_packet(packet_id: str, *, root: Path | None = None) -> ResearchPacket:
    """Read and verify a stored packet (V15, V57).

    A version-drifted packet loads and reports ``is_stale()``; a packet whose
    recomputed hash disagrees with its stored hash raises
    ``ResearchPacketTamperedError`` instead.
    """

    path = packet_path_for(packet_id, root=root)
    try:
        raw = path.read_bytes()
    except FileNotFoundError as exc:
        raise ResearchPacketMissingError(
            "no research packet exists for this merchant"
        ) from exc
    except OSError as exc:
        raise ResearchStoreError("research packet could not be read") from exc

    try:
        document = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ResearchPacketMalformedError("research packet is not valid JSON") from exc
    return ResearchPacket.from_dict(document)


def load_packet_if_present(
    packet_id: str, *, root: Path | None = None
) -> ResearchPacket | None:
    try:
        return load_packet(packet_id, root=root)
    except ResearchPacketMissingError:
        return None


def list_packet_ids(*, root: Path | None = None) -> tuple[str, ...]:
    """Every stored packet id, sorted for deterministic reporting.

    The repository-root directory is the only source of truth, so a packet is
    never discovered through an index that could drift from the files.
    """

    directory = _store_root(root) / PRIVATE_RESEARCH_DIRNAME
    try:
        entries = sorted(directory.iterdir())
    except FileNotFoundError:
        return ()
    except OSError as exc:
        raise ResearchStoreError("research directory could not be read") from exc
    return tuple(
        entry.stem
        for entry in entries
        if entry.is_file()
        and entry.suffix == ".json"
        and _PACKET_ID_PATTERN.match(entry.stem)
    )


def store_packet(packet: ResearchPacket, *, root: Path | None = None) -> Path:
    """Locked atomic create-or-replace of complete evidence (V13, V60).

    An existing packet is only ever replaced here, which is exactly the
    successful-refresh path, and the replacement returns the packet to pending
    review by discarding any record bound to the superseded hash (V52).
    """

    if not isinstance(packet, ResearchPacket):
        raise ResearchStoreError("packet must be a ResearchPacket")
    if packet.status is not PacketStatus.COMPLETE:
        raise ResearchStoreError("use store_failure_packet for a failed packet")

    path = packet_path_for(packet.packet_id, root=root)
    with research_writer_lock(root=root):
        _atomic_write(path, _canonical_bytes(packet.as_dict()))
        _discard_review_record(packet.packet_id, root=root)
    return path


def store_failure_packet(
    packet: ResearchPacket, *, root: Path | None = None
) -> Path | None:
    """Persist a typed failure only when no live evidence exists (V55, V60).

    Returns ``None`` when an active complete packet was preserved, which is how
    a failed refresh reports its failure without destroying frozen evidence.
    """

    if not isinstance(packet, ResearchPacket):
        raise ResearchStoreError("packet must be a ResearchPacket")
    if packet.status is not PacketStatus.FAILED:
        raise ResearchStoreError("store_failure_packet requires a failed packet")

    path = packet_path_for(packet.packet_id, root=root)
    with research_writer_lock(root=root):
        existing = load_packet_if_present(packet.packet_id, root=root)
        if existing is not None and existing.status is PacketStatus.COMPLETE:
            return None
        _atomic_write(path, _canonical_bytes(packet.as_dict()))
    return path


def load_review_records(*, root: Path | None = None) -> dict[str, PacketReviewRecord]:
    """Read the review artifact, keyed by packet id (V52).

    Each stored record repeats its own ``packet_id`` so a record cannot be
    silently re-pointed at another packet by editing the mapping key.
    """

    path = review_records_path(root=root)
    try:
        raw = path.read_bytes()
    except FileNotFoundError:
        return {}
    except OSError as exc:
        raise ResearchStoreError("research review artifact could not be read") from exc

    try:
        document = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ResearchPacketMalformedError(
            "research review artifact is not valid JSON"
        ) from exc
    if not isinstance(document, dict):
        raise ResearchPacketMalformedError("research review artifact must be an object")

    records: dict[str, PacketReviewRecord] = {}
    for key, value in document.items():
        if not isinstance(value, Mapping):
            raise ResearchPacketMalformedError("review record must be an object")
        record = PacketReviewRecord.from_dict(value)
        if record.packet_id != key:
            raise ResearchPacketMalformedError(
                "review record key does not match its packet_id"
            )
        records[record.packet_id] = record
    return records


def review_record_for(
    packet_id: str, *, root: Path | None = None
) -> PacketReviewRecord | None:
    return load_review_records(root=root).get(_require_packet_id(packet_id))


def record_review(record: PacketReviewRecord, *, root: Path | None = None) -> Path:
    """Store one exact-hash approval or rejection under the writer lock (V50).

    The artifact holds no page content: only the decision, its bound hashes and
    versions, a timestamp, and a bounded rejection reason (V34).
    """

    if not isinstance(record, PacketReviewRecord):
        raise ResearchStoreError("record must be a PacketReviewRecord")

    path = review_records_path(root=root)
    with research_writer_lock(root=root):
        records = load_review_records(root=root)
        records[record.packet_id] = record
        _write_review_records(path, records)
    return path


def _write_review_records(
    path: Path, records: Mapping[str, PacketReviewRecord]
) -> None:
    payload = {
        packet_id: record.as_dict() for packet_id, record in sorted(records.items())
    }
    _atomic_write(path, _canonical_bytes(payload))


def _discard_review_record(packet_id: str, *, root: Path | None = None) -> None:
    """A successful refresh returns the packet to pending review (V52, V60).

    A failed refresh never reaches this point, so the prior record survives it.
    """

    records = load_review_records(root=root)
    if packet_id not in records:
        return
    del records[packet_id]
    _write_review_records(review_records_path(root=root), records)


# --------------------------------------------------------------------------- #
# Private suggestion artifact (T9)
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class SuggestionArtifact:
    """Grouped review-only suggestions, canonically ordered (V22, V24, V34).

    Suggestions group by normalized merchant and each group is sorted by
    ``suggestion_id``, so unchanged content always serializes to identical
    bytes.
    """

    merchants: Mapping[str, tuple[CategorySuggestion, ...]]
    schema_version: str = SUGGESTION_SCHEMA_VERSION

    def __post_init__(self) -> None:
        if self.schema_version != SUGGESTION_SCHEMA_VERSION:
            raise ResearchPacketMalformedError(
                f"unsupported suggestion schema version {self.schema_version!r}"
            )
        grouped: dict[str, tuple[CategorySuggestion, ...]] = {}
        seen: set[str] = set()
        for merchant, suggestions in self.merchants.items():
            if merchant != normalize_context_text(merchant) or not merchant:
                raise ResearchPacketMalformedError(
                    "suggestion group key must be a normalized merchant"
                )
            ordered = tuple(sorted(suggestions, key=lambda item: item.suggestion_id))
            for suggestion in ordered:
                if suggestion.normalized_merchant != merchant:
                    raise ResearchPacketMalformedError(
                        "suggestion does not belong to its group merchant"
                    )
                if suggestion.suggestion_id in seen:
                    raise ResearchPacketMalformedError(
                        "suggestion_id must be unique in the artifact"
                    )
                seen.add(suggestion.suggestion_id)
            grouped[merchant] = ordered
        object.__setattr__(self, "merchants", grouped)

    def as_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "merchants": {
                merchant: [suggestion.as_dict() for suggestion in suggestions]
                for merchant, suggestions in sorted(self.merchants.items())
            },
        }

    def to_bytes(self) -> bytes:
        """Canonical bytes, so an unchanged artifact rewrites identically."""

        return _canonical_bytes(self.as_dict())

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> SuggestionArtifact:
        _exact_keys(
            data,
            frozenset({"schema_version", "merchants"}),
            "suggestion artifact",
        )
        raw = data["merchants"]
        if not isinstance(raw, Mapping):
            raise ResearchPacketMalformedError("suggestion merchants must be an object")
        merchants: dict[str, tuple[CategorySuggestion, ...]] = {}
        for key, values in raw.items():
            if not isinstance(values, (list, tuple)):
                raise ResearchPacketMalformedError("suggestion group must be a list")
            entries: list[CategorySuggestion] = []
            for item in values:
                if not isinstance(item, Mapping):
                    raise ResearchPacketMalformedError("suggestion must be an object")
                entries.append(CategorySuggestion.from_dict(item))
            merchants[str(key)] = tuple(entries)
        return cls(merchants=merchants, schema_version=str(data["schema_version"]))


def suggestion_records_path(*, root: Path | None = None) -> Path:
    """Path of the ignored private suggestion artifact (V11, V24)."""

    return _store_root(root) / SUGGESTION_FILENAME


def load_suggestion_artifact(*, root: Path | None = None) -> SuggestionArtifact:
    """Read the suggestion artifact; an absent file is an empty artifact."""

    path = suggestion_records_path(root=root)
    try:
        raw = path.read_bytes()
    except FileNotFoundError:
        return SuggestionArtifact(merchants={})
    except OSError as exc:
        raise ResearchStoreError("suggestion artifact could not be read") from exc

    try:
        document = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ResearchPacketMalformedError(
            "suggestion artifact is not valid JSON"
        ) from exc
    if not isinstance(document, Mapping):
        raise ResearchPacketMalformedError("suggestion artifact must be an object")
    return SuggestionArtifact.from_dict(document)


def load_suggestions(
    *, root: Path | None = None
) -> dict[str, tuple[CategorySuggestion, ...]]:
    """Suggestions grouped by normalized merchant (V24)."""

    return dict(load_suggestion_artifact(root=root).merchants)


def _merge_suggestion(
    existing: tuple[CategorySuggestion, ...], incoming: CategorySuggestion
) -> tuple[tuple[CategorySuggestion, ...], CategorySuggestion]:
    """Collapse identical proposals onto one record (V22, V24).

    The first rationale and citation set are retained and the affected context
    fingerprints are unioned, so every row that produced the proposal stays
    traceable without duplicating it.
    """

    for index, current in enumerate(existing):
        if current.suggestion_id != incoming.suggestion_id:
            continue
        combined = replace(
            current,
            context_fingerprints=(
                current.context_fingerprints + incoming.context_fingerprints
            ),
        )
        merged = list(existing)
        merged[index] = combined
        return tuple(merged), combined
    return (*existing, incoming), incoming


def record_suggestion(
    suggestion: CategorySuggestion, *, root: Path | None = None
) -> CategorySuggestion:
    """Persist one grouped proposal under the writer lock (V22, V24).

    Raises the store's lock error when another writer holds the repository-root
    lock, so a caller can never report a suggestion it failed to persist.
    """

    if not isinstance(suggestion, CategorySuggestion):
        raise ResearchStoreError("suggestion must be a CategorySuggestion")

    path = suggestion_records_path(root=root)
    with research_writer_lock(root=root):
        artifact = load_suggestion_artifact(root=root)
        merged, stored = _merge_suggestion(
            artifact.merchants.get(suggestion.normalized_merchant, ()), suggestion
        )
        grouped = dict(artifact.merchants)
        grouped[suggestion.normalized_merchant] = merged
        _atomic_write(path, SuggestionArtifact(merchants=grouped).to_bytes())
    return stored

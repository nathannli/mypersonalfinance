"""TinyFish research configuration, typed errors, and direct REST client.

This module owns the validated configuration, the failure hierarchy that maps
onto ``UnresolvedReason`` (V7, V55), and the Search/Fetch client (V5, V6, V9,
V10, V26-V29, V42, V43).

The client talks to the REST APIs only: no CLI subprocess, Agent, Research,
Browser, MCP, provider fallback, or alternate search provider is used (V6).
"""

from __future__ import annotations

import json
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from typing import Callable, Mapping, NamedTuple

from services.research_packets import (
    DEFAULT_RESEARCH_PURPOSE,
    MAX_FETCH_CHARS_PER_PAGE,
    MAX_SEARCH_RESULTS,
    FetchedPage,
    SearchResult,
    is_http_url,
)
from services.transaction_categorization import UnresolvedReason

DEFAULT_SEARCH_TIMEOUT_SECONDS = 30.0
DEFAULT_FETCH_TIMEOUT_SECONDS = 150.0
DEFAULT_RESEARCH_LOCATION = "CA"
DEFAULT_RESEARCH_LANGUAGE = "en"
TINYFISH_API_KEY_ENV = "TINYFISH_API_KEY"


class ResearchError(ValueError):
    """Base class for research failures.

    ``reason`` is the typed cause recorded on a failure packet. It is ``None``
    only for failures that abort the run before any merchant is known, which
    therefore never produce a packet (V55).
    """

    reason: UnresolvedReason | None = None


class ResearchConfigError(ResearchError):
    """Startup configuration failure; aborts the run before discovery (V55)."""


class ResearchAuthError(ResearchError):
    reason = UnresolvedReason.RESEARCH_AUTH


class ResearchRateLimitError(ResearchError):
    reason = UnresolvedReason.RESEARCH_RATE_LIMIT


class ResearchTimeoutError(ResearchError):
    reason = UnresolvedReason.RESEARCH_TIMEOUT


class ResearchProviderError(ResearchError):
    reason = UnresolvedReason.RESEARCH_PROVIDER_ERROR


class ResearchNoResultsError(ResearchError):
    reason = UnresolvedReason.RESEARCH_NO_RESULTS


class ResearchNoValidUrlsError(ResearchError):
    reason = UnresolvedReason.RESEARCH_NO_VALID_URLS


class ResearchFetchFailedError(ResearchError):
    reason = UnresolvedReason.RESEARCH_FETCH_FAILED


class ResearchEmptyEvidenceError(ResearchError):
    reason = UnresolvedReason.RESEARCH_EMPTY_EVIDENCE


class ResearchIrrelevantError(ResearchError):
    reason = UnresolvedReason.RESEARCH_IRRELEVANT


class ResearchMalformedError(ResearchError):
    reason = UnresolvedReason.RESEARCH_MALFORMED


class ResearchCircuitOpenError(ResearchError):
    """The run-level circuit is open, so no further request is made (V28)."""

    def __init__(self, message: str, *, cause: UnresolvedReason) -> None:
        super().__init__(message)
        self.reason = cause


@dataclass(frozen=True)
class TinyFishResearchConfig:
    """Validated research settings; the API key is never logged (V7)."""

    api_key: str
    search_timeout_seconds: float = DEFAULT_SEARCH_TIMEOUT_SECONDS
    fetch_timeout_seconds: float = DEFAULT_FETCH_TIMEOUT_SECONDS
    location: str = DEFAULT_RESEARCH_LOCATION
    language: str = DEFAULT_RESEARCH_LANGUAGE

    def __post_init__(self) -> None:
        if not isinstance(self.api_key, str) or not self.api_key.strip():
            raise ResearchConfigError("TINYFISH_API_KEY is required")

        for name, value in (
            ("search_timeout_seconds", self.search_timeout_seconds),
            ("fetch_timeout_seconds", self.fetch_timeout_seconds),
        ):
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                raise ResearchConfigError(f"{name} must be a number")
            if value <= 0:
                raise ResearchConfigError(f"{name} must be positive")

        for name, text in (("location", self.location), ("language", self.language)):
            if not isinstance(text, str) or not text.strip():
                raise ResearchConfigError(f"{name} must not be blank")

    def __repr__(self) -> str:
        return (
            "TinyFishResearchConfig(api_key='***', "
            f"search_timeout_seconds={self.search_timeout_seconds!r}, "
            f"fetch_timeout_seconds={self.fetch_timeout_seconds!r}, "
            f"location={self.location!r}, language={self.language!r})"
        )


SEARCH_ENDPOINT = "https://api.search.tinyfish.ai"
FETCH_ENDPOINT = "https://api.fetch.tinyfish.ai"

# V43: every Search and per-URL Fetch attempt is spaced to stay inside the
# documented 30 requests/minute limit instead of relying on reactive 429s.
MAX_REQUESTS_PER_MINUTE = 30
MIN_REQUEST_INTERVAL_SECONDS = 60.0 / MAX_REQUESTS_PER_MINUTE

# V27: at most 3 attempts total per request, then the run circuit opens.
MAX_REQUEST_ATTEMPTS = 3
BACKOFF_BASE_SECONDS = 1.0
MAX_BACKOFF_SECONDS = 30.0
MAX_RETRY_AFTER_SECONDS = 60.0

# V27: nonretryable and circuit-opening straight away. The spec groups 400 with
# the auth codes, and V29 maps that whole group to `research_auth`.
_AUTH_STATUSES = frozenset({400, 401, 402, 403})
_RATE_LIMIT_STATUSES = frozenset({429})
_SERVER_ERROR_STATUSES = frozenset({500, 501, 502, 503, 504})

_RETRYABLE_FETCH_MARKERS = (
    (("timeout", "timed out", "deadline"), ResearchTimeoutError),
    (("rate", "429", "too many"), ResearchRateLimitError),
    (("auth", "401", "403", "api key", "unauthor"), ResearchAuthError),
)


class HttpResponse(NamedTuple):
    """Minimal transport result so the client is testable without a network."""

    status: int
    headers: Mapping[str, str]
    body: bytes


Transport = Callable[[str, str, Mapping[str, str], bytes | None, float], HttpResponse]


def _require_client_url(value: object, field: str) -> str:
    if not is_http_url(value):
        raise ResearchMalformedError(f"{field} must be an absolute http/https URL")
    return str(value)


def _string_field(value: object) -> str:
    return value if isinstance(value, str) else ""


def _header(headers: Mapping[str, str], name: str) -> str | None:
    wanted = name.lower()
    for key, value in headers.items():
        if str(key).lower() == wanted:
            return str(value)
    return None


def _retry_after_seconds(headers: Mapping[str, str]) -> float | None:
    raw = _header(headers, "retry-after")
    if raw is None:
        return None
    try:
        seconds = float(raw.strip())
    except ValueError:
        # HTTP-date form carries no useful guidance here; bounded backoff applies.
        return None
    if seconds < 0:
        return None
    return min(seconds, MAX_RETRY_AFTER_SECONDS)


def _backoff_seconds(attempt: int, retry_after: float | None) -> float:
    if retry_after is not None:
        return retry_after
    return min(BACKOFF_BASE_SECONDS * (2 ** (attempt - 1)), MAX_BACKOFF_SECONDS)


def _classify_status(
    status: int, headers: Mapping[str, str]
) -> tuple[bool, ResearchError, float | None]:
    if status in _AUTH_STATUSES:
        return (
            False,
            ResearchAuthError(f"TinyFish rejected the request (HTTP {status})"),
            None,
        )
    if status in _RATE_LIMIT_STATUSES:
        return (
            True,
            ResearchRateLimitError(f"TinyFish rate limit reached (HTTP {status})"),
            _retry_after_seconds(headers),
        )
    if status in _SERVER_ERROR_STATUSES:
        return (
            True,
            ResearchProviderError(f"TinyFish service failure (HTTP {status})"),
            _retry_after_seconds(headers),
        )
    # Unclassified client error: terminal for this request, but not an auth or
    # config failure, so it does not open the run circuit (V27).
    return (
        False,
        ResearchProviderError(f"TinyFish request failed (HTTP {status})"),
        None,
    )


def _decode_json(body: bytes, field: str) -> object:
    try:
        return json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        # V7: never echo the payload, which could carry unrelated data.
        raise ResearchMalformedError(f"{field} was not valid JSON") from exc


def _fetch_error_text(entry: Mapping[str, object]) -> str:
    parts = [
        str(entry[key])
        for key in ("error", "type", "code", "reason", "message", "detail")
        if key in entry
    ]
    return " ".join(parts).lower()


def _fetch_error_for(entry: Mapping[str, object]) -> ResearchError:
    text = _fetch_error_text(entry)
    for markers, error_class in _RETRYABLE_FETCH_MARKERS:
        if any(marker in text for marker in markers):
            return error_class("TinyFish could not retrieve the page")
    return ResearchProviderError("TinyFish could not retrieve the page")


def _entry_matches_url(entry: Mapping[str, object], url: str) -> bool:
    return entry.get("url") == url or entry.get("final_url") == url


def _urllib_transport(
    method: str,
    url: str,
    headers: Mapping[str, str],
    body: bytes | None,
    timeout: float,
) -> HttpResponse:
    request = urllib.request.Request(
        url, data=body, headers=dict(headers), method=method
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return HttpResponse(
                status=int(response.status),
                headers=dict(response.headers),
                body=response.read(),
            )
    except urllib.error.HTTPError as error:
        try:
            payload = error.read()
        except Exception:  # pragma: no cover - body already consumed or closed
            payload = b""
        return HttpResponse(
            status=int(error.code), headers=dict(error.headers or {}), body=payload
        )


class TinyFishClient:
    """Search and Fetch over the documented REST endpoints (V6, V26-V29, V42, V43).

    Pacing, retry state, and the run circuit are per-client, so one instance
    represents one research run. A URL is never batched with another: each gets
    its own request, its own 150-second budget, and its own retry state (V42).
    """

    def __init__(
        self,
        config: TinyFishResearchConfig,
        *,
        transport: Transport | None = None,
        sleeper: Callable[[float], None] | None = None,
        monotonic: Callable[[], float] | None = None,
    ) -> None:
        if not isinstance(config, TinyFishResearchConfig):
            raise ResearchConfigError("config must be a TinyFishResearchConfig")
        self._config = config
        self._transport: Transport = transport or _urllib_transport
        self._sleeper = sleeper or time.sleep
        self._monotonic = monotonic or time.monotonic
        self._next_request_at = 0.0
        self._circuit_reason: UnresolvedReason | None = None

    @property
    def circuit_open(self) -> bool:
        return self._circuit_reason is not None

    @property
    def circuit_reason(self) -> UnresolvedReason | None:
        """The first cause that opened the circuit; stable for the whole run."""

        return self._circuit_reason

    def _guard_circuit(self) -> None:
        if self._circuit_reason is not None:
            raise ResearchCircuitOpenError(
                "research circuit is open; no further TinyFish request is made",
                cause=self._circuit_reason,
            )

    def _open_circuit(self, reason: UnresolvedReason | None) -> None:
        if reason is not None and self._circuit_reason is None:
            self._circuit_reason = reason

    def _pace(self) -> None:
        now = self._monotonic()
        if now < self._next_request_at:
            self._sleeper(self._next_request_at - now)
            now = self._next_request_at
        self._next_request_at = now + MIN_REQUEST_INTERVAL_SECONDS

    def _send(
        self,
        *,
        method: str,
        url: str,
        payload: Mapping[str, object] | None,
        timeout: float,
    ) -> HttpResponse:
        self._guard_circuit()

        headers = {"X-API-Key": self._config.api_key, "Accept": "application/json"}
        body: bytes | None = None
        if payload is not None:
            body = json.dumps(payload, separators=(",", ":"), sort_keys=True).encode(
                "utf-8"
            )
            headers["Content-Type"] = "application/json"

        attempt = 0
        while True:
            attempt += 1
            self._pace()
            try:
                response = self._transport(method, url, headers, body, timeout)
            except TimeoutError:
                retryable, failure, retry_after = (
                    True,
                    ResearchTimeoutError("TinyFish request timed out"),
                    None,
                )
            except urllib.error.URLError as error:
                if isinstance(error.reason, TimeoutError):
                    retryable, failure, retry_after = (
                        True,
                        ResearchTimeoutError("TinyFish request timed out"),
                        None,
                    )
                else:
                    retryable, failure, retry_after = (
                        True,
                        ResearchProviderError("TinyFish transport failure"),
                        None,
                    )
            except OSError:
                retryable, failure, retry_after = (
                    True,
                    ResearchProviderError("TinyFish transport failure"),
                    None,
                )
            else:
                if 200 <= response.status < 300:
                    return response
                retryable, failure, retry_after = _classify_status(
                    response.status, response.headers
                )

            if not retryable:
                if isinstance(failure, ResearchAuthError):
                    self._open_circuit(failure.reason)
                raise failure
            if attempt >= MAX_REQUEST_ATTEMPTS:
                self._open_circuit(failure.reason)
                raise failure
            self._sleeper(_backoff_seconds(attempt, retry_after))

    def search(self, derived_query: str) -> tuple[SearchResult, ...]:
        """One logical Search operation; returns ranked retained results (V5, V9)."""

        if not isinstance(derived_query, str) or not derived_query.strip():
            # V44/V53: zero significant tokens issues zero requests.
            raise ResearchIrrelevantError("derived search term must not be blank")

        query = urllib.parse.urlencode(
            {
                "query": derived_query,
                "purpose": DEFAULT_RESEARCH_PURPOSE,
                "location": self._config.location,
                "language": self._config.language,
            }
        )
        response = self._send(
            method="GET",
            url=f"{SEARCH_ENDPOINT}?{query}",
            payload=None,
            timeout=self._config.search_timeout_seconds,
        )
        return _parse_search_response(response.body)

    def fetch(self, url: str) -> FetchedPage:
        """One independent Fetch request for exactly this URL (V42)."""

        requested = _require_client_url(url, "url")
        response = self._send(
            method="POST",
            url=FETCH_ENDPOINT,
            payload={"urls": [requested]},
            timeout=self._config.fetch_timeout_seconds,
        )
        return _parse_fetch_response(response.body, requested)


def _parse_search_response(body: bytes) -> tuple[SearchResult, ...]:
    document = _decode_json(body, "search response")
    if not isinstance(document, dict):
        raise ResearchMalformedError("search response must be an object")
    raw = document.get("results")
    if not isinstance(raw, list):
        raise ResearchMalformedError("search response must carry a results list")
    if not raw:
        # V29: the provider itself found nothing.
        raise ResearchNoResultsError("search returned no results")

    retained: list[SearchResult] = []
    seen: set[str] = set()
    for item in raw:
        if not isinstance(item, dict):
            raise ResearchMalformedError("search result must be an object")
        url = item.get("url")
        if not is_http_url(url):
            # V10: only valid http/https URLs are retained; rank order is kept.
            continue
        if url in seen:
            continue
        seen.add(str(url))
        retained.append(
            SearchResult(
                # Position is the 0-based retained rank, which is what makes the
                # stored packet verifiable as ranked order.
                position=len(retained),
                site_name=_string_field(item.get("site_name")),
                title=_string_field(item.get("title")),
                snippet=_string_field(item.get("snippet")),
                url=str(url),
            )
        )
        if len(retained) >= MAX_SEARCH_RESULTS:
            break

    if not retained:
        # V29: results came back, but none carried a usable http(s) URL. This is
        # a distinct reason from ResearchNoResultsError, and collapsing both
        # into an empty tuple would make one of them unreachable.
        raise ResearchNoValidUrlsError("search returned no valid http(s) URLs")
    return tuple(retained)


def _parse_fetch_response(body: bytes, requested_url: str) -> FetchedPage:
    document = _decode_json(body, "fetch response")
    if not isinstance(document, dict):
        raise ResearchMalformedError("fetch response must be an object")
    results = document.get("results")
    errors = document.get("errors")
    if not isinstance(results, list) or not isinstance(errors, list):
        raise ResearchMalformedError(
            "fetch response must carry results and errors lists"
        )

    for item in results:
        if not isinstance(item, dict):
            raise ResearchMalformedError("fetch result must be an object")

    matching = [item for item in results if _entry_matches_url(item, requested_url)]
    if not matching and len(results) == 1 and not errors:
        # One request, one result: it can only be ours.
        matching = results
    if matching:
        return _page_from_fetch_result(matching[0], requested_url)

    for entry in errors:
        if not isinstance(entry, dict):
            raise ResearchMalformedError("fetch error must be an object")
    for entry in errors:
        if _entry_matches_url(entry, requested_url):
            raise _fetch_error_for(entry)
    if len(errors) == 1 and not results:
        raise _fetch_error_for(errors[0])

    raise ResearchMalformedError("fetch response did not mention the requested URL")


def _page_from_fetch_result(
    item: Mapping[str, object], requested_url: str
) -> FetchedPage:
    text = item.get("text")
    if not isinstance(text, str):
        raise ResearchMalformedError("fetched page text must be a string")

    # V9: content is bounded per URL before it can enter a packet.
    bounded = text[:MAX_FETCH_CHARS_PER_PAGE]
    if not bounded.strip():
        # V29: a fetch that yields zero bounded characters is not evidence.
        raise ResearchEmptyEvidenceError("fetched page yielded no bounded evidence")

    final_url = item.get("final_url")
    if not is_http_url(final_url):
        final_url = requested_url

    return FetchedPage(
        # The requested URL is kept so a page can only ever cite a retained
        # search result; final_url records where the fetch actually landed.
        url=requested_url,
        final_url=str(final_url),
        title=_string_field(item.get("title")),
        description=_string_field(item.get("description")),
        text=bounded,
    )

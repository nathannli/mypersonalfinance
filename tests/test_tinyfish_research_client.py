"""Network-free tests for the direct TinyFish REST client (T3)."""

from __future__ import annotations

import json
import unittest
import urllib.error
import urllib.parse
from types import SimpleNamespace

from services import tinyfish_research as tf
from services.research_packets import (
    MAX_FETCH_CHARS_PER_PAGE,
    MAX_SEARCH_RESULTS,
)
from services.tinyfish_research import (
    FETCH_ENDPOINT,
    MAX_BACKOFF_SECONDS,
    MAX_REQUEST_ATTEMPTS,
    MAX_RETRY_AFTER_SECONDS,
    MIN_REQUEST_INTERVAL_SECONDS,
    SEARCH_ENDPOINT,
    HttpResponse,
    ResearchAuthError,
    ResearchCircuitOpenError,
    ResearchEmptyEvidenceError,
    ResearchIrrelevantError,
    ResearchMalformedError,
    ResearchNoResultsError,
    ResearchNoValidUrlsError,
    ResearchProviderError,
    ResearchRateLimitError,
    ResearchTimeoutError,
    TinyFishClient,
    TinyFishResearchConfig,
)


def _config(**overrides) -> TinyFishResearchConfig:
    values: dict[str, object] = {"api_key": "test-key"}
    values.update(overrides)
    return TinyFishResearchConfig(**values)  # type: ignore[arg-type]


class FakeClock:
    """Deterministic time so pacing and backoff are exactly assertable."""

    def __init__(self, start: float = 0.0) -> None:
        self.now = start
        self.sleeps: list[float] = []

    def monotonic(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.now += seconds


class FakeTransport:
    def __init__(self, *responses: object) -> None:
        self.queue = list(responses)
        self.calls: list[SimpleNamespace] = []

    def __call__(
        self,
        method: str,
        url: str,
        headers: dict[str, str],
        body: bytes | None,
        timeout: float,
    ) -> HttpResponse:
        self.calls.append(
            SimpleNamespace(
                method=method,
                url=url,
                headers=dict(headers),
                body=body,
                timeout=timeout,
            )
        )
        if not self.queue:
            raise AssertionError("client made an unexpected extra request")
        item = self.queue.pop(0)
        if isinstance(item, Exception):
            raise item
        assert isinstance(item, HttpResponse)
        return item


def ok(body: bytes, status: int = 200, headers: dict[str, str] | None = None):
    return HttpResponse(status=status, headers=headers or {}, body=body)


def search_document(results: list[dict[str, object]]) -> bytes:
    return json.dumps(
        {"page": 1, "query": "q", "results": results, "total_results": len(results)}
    ).encode()


def fetch_document(
    results: list[dict[str, object]] = (),
    errors: list[dict[str, object]] = (),
) -> bytes:
    return json.dumps({"errors": list(errors), "results": list(results)}).encode()


def result_item(url: str, position: int = 1, **overrides: object) -> dict[str, object]:
    item: dict[str, object] = {
        "position": position,
        "site_name": "Example",
        "title": "Example title",
        "snippet": "Example snippet",
        "url": url,
    }
    item.update(overrides)
    return item


def page_item(
    url: str, text: str = "page body", **overrides: object
) -> dict[str, object]:
    item: dict[str, object] = {
        "author": None,
        "description": "A description",
        "final_url": url,
        "format": "markdown",
        "language": "en",
        "latency_ms": 12,
        "published_date": None,
        "text": text,
        "title": "Page title",
        "url": url,
    }
    item.update(overrides)
    return item


def client_with(*responses: object, config: TinyFishResearchConfig | None = None):
    transport = FakeTransport(*responses)
    clock = FakeClock()
    client = TinyFishClient(
        config or _config(),
        transport=transport,
        sleeper=clock.sleep,
        monotonic=clock.monotonic,
    )
    return client, transport, clock


class TestClientConfiguration(unittest.TestCase):
    def test_requires_a_validated_config(self):
        with self.assertRaises(tf.ResearchConfigError):
            TinyFishClient("not-a-config")  # type: ignore[arg-type]

    def test_endpoints_are_the_documented_rest_apis(self):
        # V6: exact endpoints, no CLI/MCP/alternate provider.
        self.assertEqual(SEARCH_ENDPOINT, "https://api.search.tinyfish.ai")
        self.assertEqual(FETCH_ENDPOINT, "https://api.fetch.tinyfish.ai")
        self.assertNotIn("subprocess", dir(tf))

    def test_api_key_is_sent_only_in_the_header_and_never_in_errors(self):
        client, transport, _ = client_with(ok(b"{}", status=401))
        with self.assertRaises(ResearchAuthError) as caught:
            client.search("acme")
        self.assertNotIn("test-key", str(caught.exception))
        self.assertEqual(transport.calls[0].headers["X-API-Key"], "test-key")


class TestSearchRequest(unittest.TestCase):
    def test_request_is_get_with_exactly_the_minimal_parameters(self):
        client, transport, _ = client_with(
            ok(search_document([result_item("https://a.example")]))
        )
        client.search("acme widgets")

        call = transport.calls[0]
        self.assertEqual(call.method, "GET")
        self.assertTrue(call.url.startswith(f"{SEARCH_ENDPOINT}?"))
        self.assertIsNone(call.body)
        self.assertEqual(call.timeout, _config().search_timeout_seconds)

        query = urllib.parse.parse_qs(urllib.parse.urlparse(call.url).query)
        # V5: only the derived term, fixed purpose, CA, and en leave the machine.
        self.assertEqual(set(query), {"query", "purpose", "location", "language"})
        self.assertEqual(query["query"], ["acme widgets"])
        self.assertEqual(query["location"], ["CA"])
        self.assertEqual(query["language"], ["en"])
        self.assertIn("personal-finance categorization", query["purpose"][0])

    def test_blank_query_issues_zero_requests(self):
        client, transport, _ = client_with()
        for blank in ("", "   "):
            with self.subTest(blank=blank):
                with self.assertRaises(ResearchIrrelevantError):
                    client.search(blank)
        self.assertEqual(transport.calls, [])


class TestSearchResponse(unittest.TestCase):
    def test_retains_ranked_results_with_zero_based_positions(self):
        client, _, _ = client_with(
            ok(
                search_document(
                    [
                        result_item("https://one.example", position=7),
                        result_item("https://two.example", position=9),
                    ]
                )
            )
        )
        results = client.search("acme")
        # Rank order is preserved and re-based so a packet stays verifiable.
        self.assertEqual([r.position for r in results], [0, 1])
        self.assertEqual(
            [r.url for r in results], ["https://one.example", "https://two.example"]
        )

    def test_drops_invalid_urls_and_keeps_rank_order(self):
        client, _, _ = client_with(
            ok(
                search_document(
                    [
                        result_item("ftp://files.example"),
                        {"title": "no url at all"},
                        result_item("javascript:void(0)"),
                        result_item("https://real.example"),
                    ]
                )
            )
        )
        results = client.search("acme")
        self.assertEqual([r.url for r in results], ["https://real.example"])
        self.assertEqual([r.position for r in results], [0])

    def test_deduplicates_urls_keeping_the_higher_rank(self):
        client, _, _ = client_with(
            ok(
                search_document(
                    [
                        result_item("https://dup.example", title="first"),
                        result_item("https://other.example"),
                        result_item("https://dup.example", title="second"),
                    ]
                )
            )
        )
        results = client.search("acme")
        self.assertEqual(
            [r.url for r in results], ["https://dup.example", "https://other.example"]
        )
        self.assertEqual(results[0].title, "first")

    def test_caps_retained_results_at_the_evidence_bound(self):
        many = [result_item(f"https://site{i}.example") for i in range(8)]
        client, _, _ = client_with(ok(search_document(many)))
        results = client.search("acme")
        self.assertEqual(len(results), MAX_SEARCH_RESULTS)
        self.assertEqual([r.position for r in results], list(range(MAX_SEARCH_RESULTS)))

    def test_provider_returning_nothing_is_no_results(self):
        # V29: the provider itself found nothing, which is a different reason
        # from results that came back with nothing usable inside them.
        client, _, _ = client_with(ok(search_document([])))
        with self.assertRaises(ResearchNoResultsError):
            client.search("acme")

    def test_results_without_a_usable_url_are_no_valid_urls(self):
        client, _, _ = client_with(
            ok(
                search_document(
                    [
                        result_item("ftp://files.example"),
                        {"title": "no url at all"},
                        result_item("javascript:void(0)"),
                    ]
                )
            )
        )
        with self.assertRaises(ResearchNoValidUrlsError):
            client.search("acme")

    def test_missing_text_fields_become_empty_strings(self):
        client, _, _ = client_with(
            ok(search_document([{"url": "https://sparse.example"}]))
        )
        page = client.search("acme")[0]
        self.assertEqual((page.site_name, page.title, page.snippet), ("", "", ""))

    def test_malformed_envelopes_raise(self):
        for name, body in (
            ("not json", b"<html>nope</html>"),
            ("top-level list", json.dumps([1, 2]).encode()),
            ("missing results", json.dumps({"page": 1}).encode()),
            ("results not a list", json.dumps({"results": "nope"}).encode()),
            ("result not an object", json.dumps({"results": [1]}).encode()),
        ):
            with self.subTest(name=name):
                client, _, _ = client_with(ok(body))
                with self.assertRaises(ResearchMalformedError):
                    client.search("acme")


class TestFetchRequest(unittest.TestCase):
    def test_posts_exactly_one_url_per_request(self):
        url = "https://one.example/page"
        client, transport, _ = client_with(ok(fetch_document([page_item(url)])))
        client.fetch(url)

        call = transport.calls[0]
        # V42: one URL per request, never batched.
        self.assertEqual(call.method, "POST")
        self.assertEqual(call.url, FETCH_ENDPOINT)
        self.assertEqual(json.loads(call.body), {"urls": [url]})
        self.assertEqual(call.timeout, _config().fetch_timeout_seconds)
        self.assertEqual(call.headers["Content-Type"], "application/json")

    def test_non_http_url_is_rejected_before_any_request(self):
        client, transport, _ = client_with()
        for bad in ("ftp://files.example", "not-a-url", "", None):
            with self.subTest(bad=bad):
                with self.assertRaises(ResearchMalformedError):
                    client.fetch(bad)  # type: ignore[arg-type]
        self.assertEqual(transport.calls, [])


class TestFetchResponse(unittest.TestCase):
    def test_successful_fetch_binds_the_requested_url(self):
        url = "https://one.example/page"
        client, _, _ = client_with(
            ok(
                fetch_document(
                    [
                        page_item(
                            url,
                            text="Bounded evidence body",
                            final_url="https://one.example/final",
                            title="Shop",
                            description="Neighbourhood shop",
                        )
                    ]
                )
            )
        )
        page = client.fetch(url)
        # V10: the page cites the retained search-result URL, not the redirect.
        self.assertEqual(page.url, url)
        self.assertEqual(page.final_url, "https://one.example/final")
        self.assertEqual(page.text, "Bounded evidence body")
        self.assertEqual(page.title, "Shop")
        self.assertEqual(page.description, "Neighbourhood shop")
        self.assertEqual(page.relevance_matched_tokens, ())

    def test_text_is_truncated_to_the_per_page_bound(self):
        url = "https://long.example"
        client, _, _ = client_with(
            ok(
                fetch_document(
                    [page_item(url, text="x" * (MAX_FETCH_CHARS_PER_PAGE * 2))]
                )
            )
        )
        page = client.fetch(url)
        self.assertEqual(len(page.text), MAX_FETCH_CHARS_PER_PAGE)

    def test_zero_bounded_characters_is_not_evidence(self):
        url = "https://blank.example"
        for text in ("", "   \n\t "):
            with self.subTest(text=repr(text)):
                client, _, _ = client_with(
                    ok(fetch_document([page_item(url, text=text)]))
                )
                with self.assertRaises(ResearchEmptyEvidenceError):
                    client.fetch(url)

    def test_non_string_text_is_malformed(self):
        url = "https://odd.example"
        client, _, _ = client_with(ok(fetch_document([page_item(url, text=None)])))
        with self.assertRaises(ResearchMalformedError):
            client.fetch(url)

    def test_invalid_final_url_falls_back_to_the_requested_url(self):
        url = "https://one.example"
        for bad in ("ftp://files.example", "", None):
            with self.subTest(bad=bad):
                client, _, _ = client_with(
                    ok(fetch_document([page_item(url, final_url=bad)]))
                )
                self.assertEqual(client.fetch(url).final_url, url)

    def test_per_url_errors_map_to_typed_reasons(self):
        url = "https://one.example"
        cases = (
            ("timeout", ResearchTimeoutError),
            ("request timed out", ResearchTimeoutError),
            ("429", ResearchRateLimitError),
            ("rate limit exceeded", ResearchRateLimitError),
            ("unauthorized", ResearchAuthError),
            ("401", ResearchAuthError),
            ("bot_blocked", ResearchProviderError),
            ("target_unreachable", ResearchProviderError),
            ("page_not_found", ResearchProviderError),
            ("content_too_large", ResearchProviderError),
            ("login_required", ResearchProviderError),
        )
        for marker, expected in cases:
            with self.subTest(marker=marker):
                client, _, _ = client_with(
                    ok(fetch_document(errors=[{"url": url, "error": marker}]))
                )
                with self.assertRaises(expected):
                    client.fetch(url)

    def test_error_entry_is_matched_by_url(self):
        url = "https://mine.example"
        client, _, _ = client_with(
            ok(
                fetch_document(
                    errors=[
                        {"url": "https://other.example", "error": "timeout"},
                        {"url": url, "error": "bot_blocked"},
                    ]
                )
            )
        )
        with self.assertRaises(ResearchProviderError) as caught:
            client.fetch(url)
        self.assertNotIsInstance(caught.exception, ResearchTimeoutError)

    def test_single_result_without_url_match_is_treated_as_ours(self):
        url = "https://requested.example"
        client, _, _ = client_with(
            ok(fetch_document([page_item("https://redirected.example", text="body")]))
        )
        # One request, one error-free result: it can only be the URL we asked for.
        page = client.fetch(url)
        self.assertEqual(page.url, url)

    def test_response_that_mentions_neither_url_is_malformed(self):
        # An empty envelope, or several results none of which can be ours.
        for body in (
            fetch_document(),
            fetch_document(
                results=[
                    page_item("https://someone-else.example"),
                    page_item("https://another.example"),
                ],
                errors=[{"url": "https://third.example", "error": "timeout"}],
            ),
        ):
            with self.subTest(body=body[:40]):
                client, _, _ = client_with(ok(body))
                with self.assertRaises(ResearchMalformedError):
                    client.fetch("https://requested.example")

    def test_malformed_fetch_envelopes_raise(self):
        for name, body in (
            ("not json", b"nope"),
            ("top-level list", json.dumps([]).encode()),
            ("missing errors", json.dumps({"results": []}).encode()),
            (
                "result not an object",
                json.dumps({"errors": [], "results": [1]}).encode(),
            ),
            (
                "error not an object",
                json.dumps({"errors": [1], "results": []}).encode(),
            ),
        ):
            with self.subTest(name=name):
                client, _, _ = client_with(ok(body))
                with self.assertRaises(ResearchMalformedError):
                    client.fetch("https://one.example")


class TestRetryPolicy(unittest.TestCase):
    def test_rate_limit_retries_then_succeeds(self):
        client, transport, _ = client_with(
            ok(b"{}", status=429, headers={"Retry-After": "5"}),
            ok(search_document([result_item("https://a.example")])),
        )
        results = client.search("acme")
        self.assertEqual(len(results), 1)
        self.assertEqual(len(transport.calls), 2)
        self.assertFalse(client.circuit_open)

    def test_retry_after_guidance_is_honoured(self):
        client, _, clock = client_with(
            ok(b"{}", status=429, headers={"Retry-After": "5"}),
            ok(search_document([result_item("https://one.example")])),
        )
        client.search("acme")
        self.assertIn(5.0, clock.sleeps)

    def test_server_errors_exhaust_attempts_then_open_the_circuit(self):
        client, transport, _ = client_with(
            ok(b"{}", status=500),
            ok(b"{}", status=500),
            ok(b"{}", status=500),
        )
        with self.assertRaises(ResearchProviderError):
            client.search("acme")
        # V27: at most 3 attempts total for this request.
        self.assertEqual(len(transport.calls), MAX_REQUEST_ATTEMPTS)
        self.assertTrue(client.circuit_open)

    def test_gateway_errors_are_retryable_too(self):
        client, transport, _ = client_with(
            ok(b"{}", status=502), ok(b"{}", status=503), ok(b"{}", status=504)
        )
        with self.assertRaises(ResearchProviderError):
            client.search("acme")
        self.assertEqual(len(transport.calls), MAX_REQUEST_ATTEMPTS)

    def test_auth_statuses_are_not_retried_and_open_the_circuit_at_once(self):
        for status in (400, 401, 402, 403):
            with self.subTest(status=status):
                client, transport, _ = client_with(ok(b"{}", status=status))
                with self.assertRaises(ResearchAuthError):
                    client.search("acme")
                self.assertEqual(len(transport.calls), 1)
                self.assertTrue(client.circuit_open)
                self.assertEqual(
                    client.circuit_reason, tf.UnresolvedReason.RESEARCH_AUTH
                )

    def test_unclassified_client_error_is_terminal_without_opening_the_circuit(self):
        client, transport, _ = client_with(ok(b"{}", status=404))
        with self.assertRaises(ResearchProviderError):
            client.search("acme")
        self.assertEqual(len(transport.calls), 1)
        self.assertFalse(client.circuit_open)

    def test_timeouts_and_transport_failures_are_retryable(self):
        cases = (
            ("timeout", TimeoutError("slow"), ResearchTimeoutError),
            (
                "urlerror-timeout",
                urllib.error.URLError(TimeoutError("slow")),
                ResearchTimeoutError,
            ),
            (
                "urlerror-refused",
                urllib.error.URLError(ConnectionRefusedError("nope")),
                ResearchProviderError,
            ),
            ("oserror", OSError("broken pipe"), ResearchProviderError),
        )
        for name, error, expected in cases:
            with self.subTest(name=name):
                client, transport, _ = client_with(error, error, error)
                with self.assertRaises(expected):
                    client.search("acme")
                self.assertEqual(len(transport.calls), MAX_REQUEST_ATTEMPTS)

    def test_backoff_is_bounded_exponential_and_yields_to_retry_after(self):
        self.assertEqual(tf._backoff_seconds(1, None), 1.0)
        self.assertEqual(tf._backoff_seconds(2, None), 2.0)
        self.assertEqual(tf._backoff_seconds(3, None), 4.0)
        self.assertEqual(tf._backoff_seconds(50, None), MAX_BACKOFF_SECONDS)
        # Explicit guidance wins over computed backoff.
        self.assertEqual(tf._backoff_seconds(1, 5.0), 5.0)
        self.assertLessEqual(tf._backoff_seconds(1, 5.0), MAX_RETRY_AFTER_SECONDS)

    def test_retry_after_parsing_is_bounded_and_ignores_unusable_forms(self):
        self.assertEqual(tf._retry_after_seconds({"Retry-After": "5"}), 5.0)
        self.assertEqual(tf._retry_after_seconds({"retry-after": "5"}), 5.0)
        self.assertEqual(
            tf._retry_after_seconds({"Retry-After": "9999"}), MAX_RETRY_AFTER_SECONDS
        )
        for unusable in ("-1", "Wed, 21 Oct 2026 07:28:00 GMT", "soon", ""):
            with self.subTest(unusable=unusable):
                self.assertIsNone(tf._retry_after_seconds({"Retry-After": unusable}))
        self.assertIsNone(tf._retry_after_seconds({}))


class TestCircuit(unittest.TestCase):
    def test_open_circuit_makes_no_further_requests(self):
        client, transport, _ = client_with(ok(b"{}", status=401))
        with self.assertRaises(ResearchAuthError):
            client.search("acme")
        calls_after_open = len(transport.calls)

        for call in (
            lambda: client.search("other"),
            lambda: client.fetch("https://a.example"),
        ):
            with self.assertRaises(ResearchCircuitOpenError) as caught:
                call()
            # V28: a stable typed failure, and not one more TinyFish call.
            self.assertEqual(caught.exception.reason, tf.UnresolvedReason.RESEARCH_AUTH)
        self.assertEqual(len(transport.calls), calls_after_open)

    def test_circuit_keeps_the_first_cause_even_after_another_failure(self):
        client, _, _ = client_with(
            ok(b"{}", status=500),
            ok(b"{}", status=500),
            ok(b"{}", status=500),
        )
        with self.assertRaises(ResearchProviderError):
            client.search("acme")
        first = client.circuit_reason
        with self.assertRaises(ResearchCircuitOpenError) as caught:
            client.search("second")
        self.assertEqual(client.circuit_reason, first)
        self.assertEqual(caught.exception.reason, first)


class TestPacing(unittest.TestCase):
    def test_requests_are_paced_to_stay_inside_the_rate_limit(self):
        url = "https://one.example"
        client, _, clock = client_with(
            ok(search_document([result_item(url)])),
            ok(search_document([result_item(url)])),
        )
        client.search("acme")
        self.assertEqual(clock.sleeps, [])  # first request is never delayed
        client.search("other")
        # V43: the second request waits out the interval rather than risking a 429.
        self.assertEqual(clock.sleeps, [MIN_REQUEST_INTERVAL_SECONDS])

    def test_interval_implies_at_most_thirty_requests_per_minute(self):
        per_minute = 60.0 / MIN_REQUEST_INTERVAL_SECONDS
        self.assertLessEqual(per_minute, 30)
        self.assertAlmostEqual(per_minute, 30)

    def test_retries_are_paced_as_well_as_backed_off(self):
        client, transport, clock = client_with(
            ok(b"{}", status=500),
            ok(search_document([result_item("https://one.example")])),
        )
        client.search("acme")
        self.assertEqual(len(transport.calls), 2)
        self.assertGreaterEqual(len(clock.sleeps), 2)
        self.assertLessEqual(max(clock.sleeps), MAX_BACKOFF_SECONDS)


class TestPerUrlBudgetIsolation(unittest.TestCase):
    """V42: each fetched URL has its own request, budget, and retry state."""

    def test_two_urls_retry_independently_within_their_own_budgets(self):
        first = "https://one.example/page"
        second = "https://two.example/page"
        client, transport, _ = client_with(
            ok(b"{}", status=500),
            ok(b"{}", status=500),
            ok(fetch_document([page_item(first, text="Alpha evidence")])),
            ok(b"{}", status=503),
            ok(b"{}", status=503),
            ok(fetch_document([page_item(second, text="Beta evidence")])),
        )

        first_page = client.fetch(first)
        second_page = client.fetch(second)

        self.assertEqual(first_page.url, first)
        self.assertEqual(second_page.url, second)
        # Two retries each: neither URL consumed the other's three attempts.
        self.assertEqual(len(transport.calls), 6)
        self.assertFalse(client.circuit_open)

    def test_each_fetch_request_uses_the_full_per_url_timeout(self):
        url = "https://one.example/page"
        client, transport, _ = client_with(
            ok(fetch_document([page_item(url, text="Alpha evidence")]))
        )

        client.fetch(url)

        self.assertEqual(transport.calls[0].timeout, 150.0)

    def test_a_url_that_exhausts_its_attempts_does_not_stop_the_next_one(self):
        first = "https://one.example/page"
        second = "https://two.example/page"
        client, transport, _ = client_with(
            ok(b"{}", status=500),
            ok(b"{}", status=500),
            ok(fetch_document([page_item(first, text="Alpha evidence")])),
            ok(fetch_document([page_item(second, text="Beta evidence")])),
        )

        client.fetch(first)
        # The first URL needed three attempts; the second still ran.
        second_page = client.fetch(second)

        self.assertEqual(second_page.url, second)
        self.assertEqual(len(transport.calls), 4)


if __name__ == "__main__":
    unittest.main()

"""Tests for the research and review CLIs (T6).

Both entry points are driven network-free. The real runner, the real stores,
and the real review logic run against a temporary root; only the TinyFish client
and the database are replaced.
"""

from __future__ import annotations

import contextlib
import importlib.util
import io
import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from services import research_runner as research_runner_module
from services.research_packets import (
    FetchedPage,
    PacketReviewStatus,
    PacketStatus,
    ResearchPacket,
    SearchResult,
    load_packet,
    review_record_for,
    store_packet,
)
from services.tinyfish_research import ResearchNoResultsError

WORKTREE_ROOT = Path(__file__).resolve().parents[1]
SEARCH_URL = "https://acme.example/about"


def load_cli_module(name: str, filename: str):
    spec = importlib.util.spec_from_file_location(name, WORKTREE_ROOT / filename)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


research_cli = load_cli_module(
    "research_cli_under_test", "research-transaction-merchants.py"
)
review_cli = load_cli_module("review_cli_under_test", "review-transaction-research.py")

CHOICES = [
    {
        "subcategory_id": 13,
        "category_id": 1,
        "subcategory_name": "Grocery",
        "category_name": "Food",
    },
]


class FakeDB:
    """Read-only stand-in for MyFinanceDB (the CLIs never write to it)."""

    def __init__(self, *, auto_match: dict | None = None, debug: bool = False):
        self.auto_match = auto_match or {}
        self.choices_reads = 0

    def get_categorization_choices(self):
        self.choices_reads += 1
        return CHOICES

    def get_auto_match_category(self, merchant: str):
        return self.auto_match.get(merchant)


class FakeClient:
    """Records every TinyFish request; fails only where a test asks."""

    def __init__(self, *, fail_queries=(), results=None, pages=None):
        self.fail_queries = set(fail_queries)
        self.results = results or (
            SearchResult(
                position=0,
                site_name="Acme",
                title="Acme Widgets",
                snippet="We make widgets",
                url=SEARCH_URL,
            ),
        )
        self.pages = pages or {
            SEARCH_URL: FetchedPage(
                url=SEARCH_URL,
                final_url=SEARCH_URL,
                title="Acme Widgets",
                description="A widget maker",
                text="Acme makes widgets for industry.",
            )
        }
        self.search_calls: list[str] = []
        self.fetch_calls: list[str] = []
        self.circuit_open = False
        self.circuit_reason = None

    def search(self, derived_query: str):
        self.search_calls.append(derived_query)
        if derived_query in self.fail_queries:
            raise ResearchNoResultsError("no results")
        return self.results

    def fetch(self, url: str):
        self.fetch_calls.append(url)
        return self.pages[url]


def rows_for(*merchants: str) -> list[dict]:
    return [
        {"merchant": merchant, "cc_category": None, "cost": 10.0}
        for merchant in merchants
    ]


class CliTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self._temporary = tempfile.TemporaryDirectory()
        self.root = Path(self._temporary.name)
        self.client = FakeClient()
        self.db = FakeDB()
        self._patches: list[tuple[object, str, object]] = []

    def tearDown(self) -> None:
        for target, name, original in reversed(self._patches):
            setattr(target, name, original)
        self._temporary.cleanup()

    def patch(self, target, name: str, value):
        original = getattr(target, name)
        self._patches.append((target, name, original))
        setattr(target, name, value)
        return value

    def run_cli(self, module, argv) -> tuple[int, str]:
        buffer = io.StringIO()
        with contextlib.redirect_stdout(buffer), contextlib.redirect_stderr(buffer):
            code = module.main(argv)
        return code, buffer.getvalue()


class ResearchCliTestCase(CliTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.patch(research_cli, "resolve_api_key", lambda: "test-key")
        self.patch(
            research_cli, "load_rows", lambda card_type, files: rows_for("Acme Widgets")
        )
        self.patch(research_cli, "MyFinanceDB", lambda **kwargs: self.db)
        self.patch(research_cli, "TinyFishClient", lambda config: self.client)
        # Bind the real runner to this test's root instead of the repository.
        self.patch(
            research_cli,
            "run_targets",
            lambda targets,
            *,
            client,
            refresh=False: research_runner_module.run_targets(
                targets, client=client, refresh=refresh, root=self.root
            ),
        )

    def research(self, *extra: str) -> tuple[int, str]:
        return self.run_cli(
            research_cli,
            [
                "--type",
                "amex",
                "--filepath",
                "/dev/null",
                "--database",
                "finance",
                *extra,
            ],
        )

    def stored_packets(self) -> list[ResearchPacket]:
        directory = self.root / ".transaction-web-research"
        if not directory.exists():
            return []
        return [
            load_packet(path.stem, root=self.root)
            for path in sorted(directory.iterdir())
        ]


class TestApiKeyResolution(CliTestCase):
    """V48: the research entry point loads the repository .env itself."""

    def test_the_key_is_read_from_the_repository_env_file(self):
        (self.root / ".env").write_text(
            "TINYFISH_API_KEY=from-repo-env\n", encoding="utf-8"
        )
        self.patch(research_cli, "repo_root", lambda: self.root)

        with patch.dict(os.environ, {}, clear=True):
            key = research_cli.resolve_api_key()

        self.assertEqual(key, "from-repo-env")

    def test_the_resolved_key_is_stripped(self):
        (self.root / ".env").write_text(
            'TINYFISH_API_KEY="  padded-key  "\n', encoding="utf-8"
        )
        self.patch(research_cli, "repo_root", lambda: self.root)

        with patch.dict(os.environ, {}, clear=True):
            key = research_cli.resolve_api_key()

        self.assertEqual(key, "padded-key")

    def test_a_missing_key_names_the_env_path(self):
        self.patch(research_cli, "repo_root", lambda: self.root)

        with patch.dict(os.environ, {}, clear=True):
            with self.assertRaises(ValueError) as raised:
                research_cli.resolve_api_key()

        self.assertIn("TINYFISH_API_KEY", str(raised.exception))
        self.assertIn(".env", str(raised.exception))


class TestResearchCliGuardRails(ResearchCliTestCase):
    def test_rejects_a_database_other_than_finance(self):
        code, output = self.run_cli(
            research_cli,
            [
                "--type",
                "amex",
                "--filepath",
                "/dev/null",
                "--database",
                "parents_finance",
            ],
        )
        self.assertEqual(code, 1)
        self.assertIn("finance only", output)
        self.assertEqual(self.client.search_calls, [])

    def test_missing_api_key_fails_before_any_request(self):
        def missing():
            raise ValueError("TINYFISH_API_KEY is required")

        self.patch(research_cli, "resolve_api_key", missing)
        code, output = self.research()
        self.assertEqual(code, 1)
        self.assertIn("TINYFISH_API_KEY", output)
        self.assertEqual(self.client.search_calls, [])
        # V55: a pre-discovery failure mutates no packet and no review record.
        self.assertEqual(self.stored_packets(), [])
        self.assertFalse(
            (self.root / ".transaction-web-research-approvals.json").exists()
        )

    def test_input_failure_fails_the_run_without_researching(self):
        def boom(card_type, files):
            raise ValueError("unreadable statement")

        self.patch(research_cli, "load_rows", boom)
        code, _ = self.research()
        self.assertEqual(code, 1)
        self.assertEqual(self.client.search_calls, [])
        self.assertEqual(self.client.fetch_calls, [])
        self.assertEqual(self.stored_packets(), [])
        self.assertFalse(
            (self.root / ".transaction-web-research-approvals.json").exists()
        )

    def test_both_filepath_and_folder_is_rejected(self):
        code, output = self.run_cli(
            research_cli,
            [
                "--type",
                "amex",
                "--filepath",
                "/dev/null",
                "--folder",
                "/tmp",
                "--database",
                "finance",
            ],
        )
        self.assertEqual(code, 1)
        self.assertIn("Cannot provide both", output)

    def test_api_key_is_required_only_by_this_entry_point(self):
        # V39: the key is resolved lazily here and never during Config.
        source = (WORKTREE_ROOT / "config.py").read_text(encoding="utf-8")
        self.assertNotIn("TINYFISH", source)
        research_source = (
            WORKTREE_ROOT / "research-transaction-merchants.py"
        ).read_text(encoding="utf-8")
        self.assertIn("load_dotenv", research_source)
        self.assertIn("TINYFISH_API_KEY_ENV", research_source)


class TestResearchCliRun(ResearchCliTestCase):
    def test_no_targets_is_complete_and_makes_no_request(self):
        self.patch(
            research_cli,
            "MyFinanceDB",
            lambda **kwargs: FakeDB(auto_match={"Acme Widgets": ("Food", "Grocery")}),
        )
        code, output = self.research()
        self.assertEqual(code, 0)
        self.assertIn("COMPLETE", output)
        self.assertIn("Nothing to research", output)
        self.assertEqual(self.client.search_calls, [])
        self.assertEqual(self.stored_packets(), [])

    def test_successful_research_exits_zero_and_stores_a_pending_packet(self):
        code, output = self.research()
        self.assertEqual(code, 0)
        self.assertIn("COMPLETE", output)
        self.assertEqual(len(self.client.search_calls), 1)

        packets = self.stored_packets()
        self.assertEqual(len(packets), 1)
        self.assertIs(packets[0].status, PacketStatus.COMPLETE)
        self.assertIsNone(review_record_for(packets[0].packet_id, root=self.root))

    def test_every_target_failing_exits_one(self):
        self.patch(research_cli, "load_rows", lambda c, f: rows_for("Broken Widgets"))
        self.patch(
            research_cli,
            "TinyFishClient",
            lambda config: FakeClient(fail_queries={"broken widgets"}),
        )
        code, output = self.research()
        self.assertEqual(code, 1)
        self.assertIn("FAILED", output)
        self.assertIn("research_no_results", output)

    def test_one_failure_among_successes_exits_zero_as_partial(self):
        self.patch(
            research_cli,
            "load_rows",
            lambda c, f: rows_for("Acme Widgets", "Broken Widgets"),
        )
        self.patch(
            research_cli,
            "TinyFishClient",
            lambda config: FakeClient(fail_queries={"broken widgets"}),
        )
        code, output = self.research()
        self.assertEqual(code, 0)
        self.assertIn("PARTIAL", output)

    def test_refresh_flag_produces_a_refresh_operation(self):
        first, _ = self.research()
        self.assertEqual(first, 0)
        second, output = self.research("--refresh")
        self.assertEqual(second, 0)
        self.assertIn("refreshed", output)

    def test_second_run_without_refresh_reuses_the_packet(self):
        self.research()
        calls_before = len(self.client.search_calls)
        code, output = self.research()
        self.assertEqual(code, 0)
        self.assertIn("reused", output)
        # V13: an existing valid packet is reused, not researched again.
        self.assertEqual(len(self.client.search_calls), calls_before)

    def test_output_never_contains_the_api_key_or_page_text(self):
        self.patch(research_cli, "resolve_api_key", lambda: "super-secret-key")
        code, output = self.research()
        self.assertEqual(code, 0)
        self.assertNotIn("super-secret-key", output)
        # V34: no raw page content is echoed into the terminal summary.
        self.assertNotIn("Acme makes widgets for industry.", output)

    def test_taxonomy_is_read_once_per_run(self):
        self.patch(
            research_cli,
            "load_rows",
            lambda c, f: rows_for("Acme Widgets", "Beta Widgets", "Gamma Widgets"),
        )
        self.research()
        # V43: the live taxonomy is read once and reused for every row.
        self.assertEqual(self.db.choices_reads, 1)


class ReviewCliTestCase(CliTestCase):
    """The review CLI runs against real stores bound to a temporary root."""

    def setUp(self) -> None:
        super().setUp()
        from services import research_packets as packets

        self.patch(
            review_cli,
            "list_packet_ids",
            lambda: packets.list_packet_ids(root=self.root),
        )
        self.patch(
            review_cli,
            "load_packet",
            lambda pid: packets.load_packet(pid, root=self.root),
        )
        self.patch(
            review_cli,
            "review_record_for",
            lambda pid: packets.review_record_for(pid, root=self.root),
        )
        self.patch(
            review_cli,
            "record_review",
            lambda record: packets.record_review(record, root=self.root),
        )
        self.packet = self.store_packet_for("acme widgets")

    def store_packet_for(self, merchant: str, **overrides) -> ResearchPacket:
        values = {
            "normalized_merchant": merchant,
            "derived_query": merchant,
            "status": PacketStatus.COMPLETE,
            "searched_at": "2026-09-17T12:00:00+00:00",
            "search_results": (
                SearchResult(
                    position=0,
                    site_name="Acme",
                    title="Acme Widgets",
                    snippet="We make widgets",
                    url=SEARCH_URL,
                ),
            ),
            "fetched_pages": (
                FetchedPage(
                    url=SEARCH_URL,
                    final_url=SEARCH_URL,
                    title="Acme Widgets",
                    description="A widget maker",
                    text="Acme makes widgets for industry.",
                    relevance_matched_tokens=("acme", "widgets"),
                ),
            ),
        }
        values.update(overrides)
        packet = ResearchPacket(**values)
        store_packet(packet, root=self.root)
        return packet


class TestReviewCli(ReviewCliTestCase):
    def test_empty_state_lists_nothing(self):
        shutil.rmtree(self.root / ".transaction-web-research", ignore_errors=True)
        code, output = self.run_cli(review_cli, [])
        self.assertEqual(code, 0)
        self.assertIn("No research packets found", output)

    def test_lists_pending_packets_with_evidence(self):
        code, output = self.run_cli(review_cli, [])
        self.assertEqual(code, 0)
        self.assertIn("acme widgets", output)
        self.assertIn(self.packet.packet_id, output)
        self.assertIn(SEARCH_URL, output)
        self.assertIn("Acme Widgets", output)
        self.assertIn("relevance tokens", output)
        self.assertIn("1 packet(s) awaiting review", output)

    def test_approve_binds_the_exact_packet_hash(self):
        code, output = self.run_cli(review_cli, ["--approve", self.packet.packet_id])
        self.assertEqual(code, 0)
        self.assertIn("APPROVED", output)

        record = review_record_for(self.packet.packet_id, root=self.root)
        assert record is not None
        self.assertIs(record.status, PacketReviewStatus.APPROVED)
        self.assertEqual(record.packet_sha256, self.packet.packet_sha256)

    def test_approved_packet_leaves_the_pending_list(self):
        self.run_cli(review_cli, ["--approve", self.packet.packet_id])
        code, output = self.run_cli(review_cli, [])
        self.assertEqual(code, 0)
        self.assertIn("Nothing awaiting review", output)

    def test_reject_records_a_bounded_reason(self):
        code, output = self.run_cli(
            review_cli,
            ["--reject", self.packet.packet_id, "--reason", "different company"],
        )
        self.assertEqual(code, 0)
        self.assertIn("REJECTED", output)

        record = review_record_for(self.packet.packet_id, root=self.root)
        assert record is not None
        self.assertIs(record.status, PacketReviewStatus.REJECTED)
        self.assertEqual(record.reason, "different company")

    def test_reject_without_reason_is_refused(self):
        code, output = self.run_cli(review_cli, ["--reject", self.packet.packet_id])
        self.assertEqual(code, 1)
        self.assertIn("--reason", output)
        self.assertIsNone(review_record_for(self.packet.packet_id, root=self.root))

    def test_both_approve_and_reject_is_refused(self):
        code, output = self.run_cli(
            review_cli,
            ["--approve", self.packet.packet_id, "--reject", self.packet.packet_id],
        )
        self.assertEqual(code, 1)
        self.assertIn("not both", output)

    def test_reason_without_reject_is_refused(self):
        code, output = self.run_cli(review_cli, ["--reason", "why"])
        self.assertEqual(code, 1)
        self.assertIn("only valid with --reject", output)

    def test_unknown_packet_is_refused(self):
        code, output = self.run_cli(review_cli, ["--approve", "f" * 64])
        self.assertEqual(code, 1)
        self.assertIn("no stored packet matches", output)

    def test_a_unique_prefix_is_accepted(self):
        code, _ = self.run_cli(review_cli, ["--approve", self.packet.packet_id[:12]])
        self.assertEqual(code, 0)
        self.assertIsNotNone(review_record_for(self.packet.packet_id, root=self.root))

    def test_failure_packets_are_never_reviewable(self):
        # V55: a typed failure packet carries no evidence to approve.
        from services.research_packets import store_failure_packet

        failure = ResearchPacket(
            normalized_merchant="broken widgets",
            derived_query="broken widgets",
            status=PacketStatus.FAILED,
            searched_at="2026-09-17T12:00:00+00:00",
            failure_reason=research_runner_module.UnresolvedReason.RESEARCH_TIMEOUT,
        )
        store_failure_packet(failure, root=self.root)

        code, output = self.run_cli(review_cli, ["--approve", failure.packet_id])
        self.assertEqual(code, 1)
        self.assertIn("never reviewable", output)
        self.assertIsNone(review_record_for(failure.packet_id, root=self.root))

        # It is also not offered as pending.
        _, listing = self.run_cli(review_cli, [])
        self.assertIn("1 failed", listing)

    def test_stale_packets_require_a_refresh_first(self):
        # V57: version drift blocks review until a refresh produces new hashes.
        from services.research_packets import ResearchPacket as Packet

        stale = Packet(
            normalized_merchant="stale widgets",
            derived_query="stale widgets",
            status=PacketStatus.COMPLETE,
            searched_at="2026-09-17T12:00:00+00:00",
            schema_version="transaction-web-research-v0",
            search_results=self.packet.search_results,
            fetched_pages=self.packet.fetched_pages,
        )
        store_packet(stale, root=self.root)

        code, output = self.run_cli(review_cli, ["--approve", stale.packet_id])
        self.assertEqual(code, 1)
        self.assertIn("different schema/query versions", output)
        self.assertIsNone(review_record_for(stale.packet_id, root=self.root))

    def test_a_tampered_packet_is_reported_not_approved(self):
        path = self.root / ".transaction-web-research" / f"{self.packet.packet_id}.json"
        document = path.read_text(encoding="utf-8").replace(
            '"derived_query":"acme widgets"', '"derived_query":"something else"'
        )
        path.write_text(document, encoding="utf-8")

        code, output = self.run_cli(review_cli, ["--approve", self.packet.packet_id])
        self.assertEqual(code, 1)
        # The store's own tamper detection refuses it before any approval.
        self.assertIn("content hash does not match", output.lower())
        self.assertIsNone(review_record_for(self.packet.packet_id, root=self.root))

    def test_review_cli_is_offline(self):
        """V3/V35: no TinyFish, OpenCodex, or database module is reachable.

        Checked structurally against the entry point's own imports rather than
        ``sys.modules``, because the research CLI legitimately imports the
        database and would otherwise pollute this check.
        """

        import ast

        source = (WORKTREE_ROOT / "review-transaction-research.py").read_text(
            encoding="utf-8"
        )
        modules: set[str] = set()
        for node in ast.walk(ast.parse(source)):
            if isinstance(node, ast.Import):
                modules.update(alias.name for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module:
                modules.add(node.module)

        forbidden_roots = {"db", "cli", "config", "psycopg", "dotenv", "requests"}
        for module in sorted(modules):
            with self.subTest(module=module):
                self.assertNotIn(module.split(".")[0], forbidden_roots)
                self.assertNotIn(
                    module,
                    {
                        "services.tinyfish_research",
                        "services.llm_categorizer",
                        "services.research_runner",
                    },
                )
        self.assertIn("services.research_packets", modules)


if __name__ == "__main__":
    unittest.main()


class TestReviewListingPendingDetection(ReviewCliTestCase):
    """Pending means "no record bound to this exact hash", not "no record"."""

    def test_stale_review_record_does_not_hide_a_pending_packet(self):
        from services import research_packets as packets

        packets.record_review(
            packets.PacketReviewRecord(
                packet_id=self.packet.packet_id,
                packet_sha256="0" * 64,
                status=PacketReviewStatus.APPROVED,
                reviewed_at="2026-09-17T13:00:00+00:00",
                reason=None,
            ),
            root=self.root,
        )

        code, output = self.run_cli(review_cli, [])

        self.assertEqual(code, 0)
        self.assertIn("1 pending review", output)
        self.assertIn("0 approved", output)
        self.assertIn("1 packet(s) awaiting review", output)

    def test_is_pending_is_false_only_for_a_record_on_the_exact_hash(self):
        from services import research_packets as packets

        self.assertTrue(review_cli.is_pending(self.packet))

        record = packets.PacketReviewRecord(
            packet_id=self.packet.packet_id,
            packet_sha256=self.packet.packet_sha256,
            status=PacketReviewStatus.APPROVED,
            reviewed_at="2026-09-17T13:00:00+00:00",
            reason=None,
        )
        packets.record_review(record, root=self.root)
        self.assertFalse(review_cli.is_pending(self.packet))

        packets.record_review(
            packets.PacketReviewRecord(
                packet_id=self.packet.packet_id,
                packet_sha256="0" * 64,
                status=PacketReviewStatus.APPROVED,
                reviewed_at="2026-09-17T13:00:00+00:00",
                reason=None,
            ),
            root=self.root,
        )
        self.assertTrue(review_cli.is_pending(self.packet))


class TestReviewSuggestionsSurface(ReviewCliTestCase):
    """`--suggestions` is the only shipped reader of the suggestion artifact.

    `suggest_new` is the one enriched action that persists anything, so without
    a read surface the grouped artifact is write-only and its V22/V24 grouping,
    sort, and dedup rules are never observed outside tests.
    """

    def setUp(self) -> None:
        super().setUp()
        from services import research_packets as packets

        self.packets = packets
        self.patch(
            review_cli,
            "load_suggestions",
            lambda: packets.load_suggestions(root=self.root),
        )

    def store_suggestion(self, merchant: str = "acme widgets"):
        return self.packets.record_suggestion(
            self.packets.CategorySuggestion(
                normalized_merchant=merchant,
                category_name="Hobbies",
                subcategory_name="Model kits",
                rationale="The evidence describes a scale-model retailer.",
                evidence_urls=(SEARCH_URL,),
                research_packet_sha256=self.packet.packet_sha256,
                context_fingerprints=("a" * 64,),
            ),
            root=self.root,
        )

    def test_empty_artifact_reports_nothing_recorded(self):
        code, output = self.run_cli(review_cli, ["--suggestions"])
        self.assertEqual(code, 0)
        self.assertIn("Category suggestions: 0 across 0 merchant(s)", output)
        self.assertIn("No category suggestions recorded", output)

    def test_renders_the_grouped_proposal_with_its_citations(self):
        stored = self.store_suggestion()

        code, output = self.run_cli(review_cli, ["--suggestions"])

        self.assertEqual(code, 0)
        self.assertIn("Category suggestions: 1 across 1 merchant(s)", output)
        self.assertIn("acme widgets", output)
        self.assertIn(stored.suggestion_id, output)
        self.assertIn("Hobbies", output)
        self.assertIn("Model kits", output)
        self.assertIn(SEARCH_URL, output)
        self.assertIn(self.packet.packet_sha256, output)
        self.assertIn("review-only", output)

    def test_grouping_is_reported_per_merchant(self):
        self.store_suggestion("acme widgets")
        self.store_suggestion("globex supplies")

        code, output = self.run_cli(review_cli, ["--suggestions"])

        self.assertEqual(code, 0)
        self.assertIn("Category suggestions: 2 across 2 merchant(s)", output)
        self.assertIn("acme widgets (1)", output)
        self.assertIn("globex supplies (1)", output)

    def test_refuses_to_combine_with_a_decision(self):
        code, output = self.run_cli(
            review_cli, ["--suggestions", "--approve", self.packet.packet_id]
        )
        self.assertEqual(code, 1)
        self.assertIn("cannot be combined", output)

    def test_a_damaged_artifact_reports_the_error_and_exits_one(self):
        (self.root / ".transaction-category-suggestions.json").write_text(
            "{ not json", encoding="utf-8"
        )

        code, output = self.run_cli(review_cli, ["--suggestions"])

        self.assertEqual(code, 1)
        self.assertIn("ERROR", output)

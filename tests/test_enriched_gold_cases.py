"""Tests for the packet-bound enriched gold set (T10).

Pins V30/V32/V46/V47/V49/V54: every enriched gold case binds an exact approved
research packet, parses against one fixed exact schema, and compares
action-specific expectations. A missing, rejected, stale, tampered, or
unapproved packet can never satisfy approval.
"""

from __future__ import annotations

import json
import subprocess
import sys
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from services.enriched_categorization import resolve_approved_packet
from services.gold_validator import describe_enriched_decision, evaluate_enriched_case
from services.llm_categorizer import (
    EnrichedAbstain,
    EnrichedSelect,
    EnrichedSuggestion,
)
from services.research_packets import (
    FetchedPage,
    PacketReviewRecord,
    PacketReviewStatus,
    PacketStatus,
    ResearchPacket,
    SearchResult,
    packet_path_for,
    record_review,
    store_packet,
    utc_now,
)
from services.transaction_categorization import (
    CanonicalContext,
    UnresolvedReason,
    build_canonical_context,
    canonicalize_choices,
)
from services.transaction_llm_approval import (
    ENRICHED_GOLD_CASE_FIELDS,
    ApprovalError,
    load_enriched_gold_cases,
    parse_enriched_gold_cases,
)

REPO_ROOT = Path(__file__).resolve().parents[1]
FIXTURE = (
    REPO_ROOT / "tests" / "fixtures" / "transaction_llm_gold_enriched_synthetic.json"
)
PACKET_HASH = "b" * 64
CONTEXT_FINGERPRINT = "f" * 64
EVIDENCE_URL = "https://beta.example/"


def load_fixture() -> dict:
    return json.loads(FIXTURE.read_text(encoding="utf-8"))


def taxonomy() -> list[dict]:
    return load_fixture()["taxonomies"]["finance"]


def fixture_cases() -> list[dict]:
    return load_fixture()["cases"]


def build_packet(raw: dict) -> ResearchPacket:
    return ResearchPacket(
        normalized_merchant=raw["normalized_merchant"],
        derived_query=raw["derived_query"],
        status=PacketStatus(raw["status"]),
        searched_at=raw["searched_at"],
        search_results=tuple(SearchResult(**item) for item in raw["search_results"]),
        fetched_pages=tuple(
            FetchedPage(
                url=page["url"],
                final_url=page["final_url"],
                title=page["title"],
                description=page["description"],
                text=page["text"],
                relevance_matched_tokens=tuple(page["relevance_matched_tokens"]),
            )
            for page in raw["fetched_pages"]
        ),
    )


def copy_case(action: str) -> dict:
    for raw in fixture_cases():
        if raw["expected_action"] == action:
            return json.loads(json.dumps(raw))
    raise AssertionError(f"fixture has no {action} case")


def case_with_parent() -> dict:
    for raw in fixture_cases():
        if raw.get("expected_parent_category_id") is not None:
            return json.loads(json.dumps(raw))
    raise AssertionError("fixture has no suggest_new case with a parent")


def parse_one(raw_case: dict, database: str = "finance"):
    document = load_fixture()
    document["cases"] = [raw_case]
    return parse_enriched_gold_cases(document, database)[0]


class TestEnrichedGoldParsing(unittest.TestCase):
    def test_every_fixture_case_uses_the_one_fixed_exact_schema(self):
        cases = fixture_cases()

        self.assertEqual(len(cases), 4)
        for raw in cases:
            with self.subTest(action=raw["expected_action"]):
                self.assertEqual(set(raw), ENRICHED_GOLD_CASE_FIELDS)

    def test_all_three_actions_parse(self):
        document = load_fixture()

        cases = parse_enriched_gold_cases(document, "finance")

        self.assertEqual(
            [case.expected_action for case in cases],
            ["select", "suggest_new", "suggest_new", "abstain"],
        )
        for case in cases:
            self.assertEqual(case.database, "finance")
            self.assertEqual(len(case.research_packet_sha256), 64)
            self.assertEqual(case.packet_schema_version, "transaction-web-research-v1")
            self.assertEqual(case.packet_query_version, "merchant-research-v2")

    def test_select_case_binds_only_a_choice(self):
        case = parse_one(copy_case("select"))

        self.assertEqual(case.expected_choice_id, 101)
        self.assertIsNone(case.expected_category_name)
        self.assertIsNone(case.expected_subcategory_name)
        self.assertIsNone(case.expected_parent_category_id)
        self.assertEqual(case.expected_evidence_urls, ())

    def test_suggest_new_case_binds_a_proposal_and_citations(self):
        case = parse_one(copy_case("suggest_new"))

        self.assertIsNone(case.expected_choice_id)
        self.assertEqual(case.expected_category_name, "Sample Hobbies")
        self.assertEqual(case.expected_subcategory_name, "Sample Widgets")
        self.assertIsNone(case.expected_parent_category_id)
        self.assertEqual(case.expected_evidence_urls, (EVIDENCE_URL,))

    def test_suggest_new_case_may_propose_under_an_existing_parent(self):
        case = parse_one(case_with_parent())

        self.assertEqual(case.expected_parent_category_id, 11)
        self.assertEqual(case.expected_category_name, "Sample Food")

    def test_abstain_case_carries_no_expectation(self):
        case = parse_one(copy_case("abstain"))

        self.assertIsNone(case.expected_choice_id)
        self.assertIsNone(case.expected_category_name)
        self.assertIsNone(case.expected_subcategory_name)
        self.assertIsNone(case.expected_parent_category_id)
        self.assertEqual(case.expected_evidence_urls, ())

    def test_pre_packet_case_is_rejected_not_reused(self):
        raw = copy_case("select")
        del raw["research_packet_sha256"]

        with self.assertRaises(ApprovalError) as raised:
            parse_one(raw)

        # V46: a finance case without packet identity is a pre-packet case.
        self.assertIn("predates packet identity", str(raised.exception))

    def test_parents_finance_is_never_enriched(self):
        with self.assertRaises(ApprovalError):
            parse_enriched_gold_cases(load_fixture(), "parents_finance")

    def test_unknown_action_is_rejected(self):
        raw = copy_case("abstain")
        raw["expected_action"] = "delete"

        with self.assertRaises(ApprovalError):
            parse_one(raw)

    def test_missing_field_is_rejected(self):
        for field in sorted(ENRICHED_GOLD_CASE_FIELDS):
            # `research_packet_sha256` has its own explicit pre-packet rejection
            # and `database` selects the case rather than describing it.
            if field in ("research_packet_sha256", "database"):
                continue
            with self.subTest(field=field):
                raw = copy_case("select")
                del raw[field]

                with self.assertRaises(ApprovalError):
                    parse_one(raw)

    def test_a_case_without_a_database_is_not_claimed_by_finance(self):
        raw = copy_case("select")
        del raw["database"]
        document = load_fixture()
        document["cases"] = [raw]

        self.assertEqual(parse_enriched_gold_cases(document, "finance"), [])

    def test_extra_field_is_rejected(self):
        raw = copy_case("select")
        raw["expected_rationale"] = "not stored in gold"

        with self.assertRaises(ApprovalError) as raised:
            parse_one(raw)

        self.assertIn("invalid fields", str(raised.exception))

    def test_packet_versions_are_required(self):
        for field in ("packet_schema_version", "packet_query_version"):
            with self.subTest(field=field):
                raw = copy_case("select")
                raw[field] = None

                with self.assertRaises(ApprovalError):
                    parse_one(raw)

    def test_packet_hash_must_be_a_lowercase_64_hex_digest(self):
        for bad in ("short", "Z" * 64, "A" * 64, 12345):
            with self.subTest(value=bad):
                raw = copy_case("select")
                raw["research_packet_sha256"] = bad

                with self.assertRaises(ApprovalError):
                    parse_one(raw)

    def test_duplicate_fingerprints_are_rejected(self):
        raw = copy_case("select")
        document = load_fixture()
        document["cases"] = [raw, dict(raw)]

        with self.assertRaises(ApprovalError):
            parse_enriched_gold_cases(document, "finance")


class TestActionSpecificNullRules(unittest.TestCase):
    def test_bool_choice_id_is_rejected(self):
        raw = copy_case("select")
        raw["expected_choice_id"] = True

        with self.assertRaises(ApprovalError):
            parse_one(raw)

    def test_select_requires_a_choice_id(self):
        raw = copy_case("select")
        raw["expected_choice_id"] = None

        with self.assertRaises(ApprovalError):
            parse_one(raw)

    def test_select_must_not_carry_suggestion_fields(self):
        for field, value in (
            ("expected_category_name", "Sample Hobbies"),
            ("expected_subcategory_name", "Sample Widgets"),
            ("expected_parent_category_id", 11),
        ):
            with self.subTest(field=field):
                raw = copy_case("select")
                raw[field] = value

                with self.assertRaises(ApprovalError):
                    parse_one(raw)

    def test_select_must_not_carry_evidence(self):
        raw = copy_case("select")
        raw["expected_evidence_urls"] = [EVIDENCE_URL]

        with self.assertRaises(ApprovalError):
            parse_one(raw)

    def test_abstain_must_not_carry_any_expectation(self):
        for field, value in (
            ("expected_choice_id", 101),
            ("expected_category_name", "Sample Hobbies"),
            ("expected_subcategory_name", "Sample Widgets"),
            ("expected_parent_category_id", 11),
        ):
            with self.subTest(field=field):
                raw = copy_case("abstain")
                raw[field] = value

                with self.assertRaises(ApprovalError):
                    parse_one(raw)

    def test_abstain_must_not_carry_evidence(self):
        raw = copy_case("abstain")
        raw["expected_evidence_urls"] = [EVIDENCE_URL]

        with self.assertRaises(ApprovalError):
            parse_one(raw)

    def test_suggest_new_must_not_carry_a_choice_id(self):
        raw = copy_case("suggest_new")
        raw["expected_choice_id"] = 101

        with self.assertRaises(ApprovalError):
            parse_one(raw)

    def test_suggest_new_requires_both_names(self):
        for field in ("expected_category_name", "expected_subcategory_name"):
            with self.subTest(field=field):
                raw = copy_case("suggest_new")
                raw[field] = None

                with self.assertRaises(ApprovalError):
                    parse_one(raw)

    def test_suggest_new_parent_id_accepts_null_or_an_integer(self):
        raw = copy_case("suggest_new")
        raw["expected_parent_category_id"] = True

        with self.assertRaises(ApprovalError):
            parse_one(raw)


class TestGoldTextBounds(unittest.TestCase):
    def test_blank_name_is_rejected(self):
        raw = copy_case("suggest_new")
        raw["expected_category_name"] = "   "

        with self.assertRaises(ApprovalError):
            parse_one(raw)

    def test_control_characters_are_rejected(self):
        raw = copy_case("suggest_new")
        raw["expected_subcategory_name"] = "Widgets\x00"

        with self.assertRaises(ApprovalError):
            parse_one(raw)

    def test_over_long_name_is_rejected(self):
        raw = copy_case("suggest_new")
        raw["expected_category_name"] = "x" * 81

        with self.assertRaises(ApprovalError):
            parse_one(raw)

    def test_names_are_whitespace_collapsed(self):
        raw = copy_case("suggest_new")
        raw["expected_category_name"] = "  Sample   Hobbies "

        case = parse_one(raw)

        self.assertEqual(case.expected_category_name, "Sample Hobbies")

    def test_evidence_url_count_is_bounded(self):
        for urls in ([], [EVIDENCE_URL] * 4):
            with self.subTest(count=len(urls)):
                raw = copy_case("suggest_new")
                raw["expected_evidence_urls"] = urls

                with self.assertRaises(ApprovalError):
                    parse_one(raw)

    def test_evidence_urls_must_be_unique_http(self):
        cases = {
            "duplicate": [EVIDENCE_URL, EVIDENCE_URL],
            "not-http": ["ftp://beta.example/"],
            "not-a-url": ["beta.example"],
            "not-a-string": [123],
        }
        for label, urls in cases.items():
            with self.subTest(label=label):
                raw = copy_case("suggest_new")
                raw["expected_evidence_urls"] = urls

                with self.assertRaises(ApprovalError):
                    parse_one(raw)

    def test_evidence_urls_must_be_a_list_or_null(self):
        raw = copy_case("suggest_new")
        raw["expected_evidence_urls"] = EVIDENCE_URL

        with self.assertRaises(ApprovalError):
            parse_one(raw)


class TestPacketBoundFingerprint(unittest.TestCase):
    def test_case_context_carries_its_packet_identity(self):
        case = parse_one(copy_case("select"))

        context = case.build_context(taxonomy())

        self.assertEqual(
            context.as_dict()["research_packet_sha256"], case.research_packet_sha256
        )

    def test_fingerprint_changes_with_packet_identity(self):
        raw = copy_case("select")
        original = raw["research_packet_sha256"]
        cases = parse_enriched_gold_cases(load_fixture(), "finance")

        # The same transaction under a different packet is a different case.
        other = CanonicalContext(
            database="finance",
            merchant="example alpha cafe",
            amount_minor_units=1250,
            statement_category="sample restaurants",
            allowed_choices=canonicalize_choices("finance", taxonomy()),
            research_packet_sha256=PACKET_HASH,
        )
        self.assertNotEqual(
            other.fingerprint,
            next(c for c in cases if c.research_packet_sha256 == original).fingerprint,
        )

    def test_unenriched_contexts_omit_packet_identity(self):
        # V2: unenriched and parents_finance fingerprints stay byte-identical.
        unenriched = CanonicalContext(
            database="finance",
            merchant="example alpha cafe",
            amount_minor_units=1250,
            statement_category="sample restaurants",
            allowed_choices=canonicalize_choices("finance", taxonomy()),
        )
        built = build_canonical_context(
            database="finance",
            merchant="example alpha cafe",
            amount=12.50,
            statement_category="sample restaurants",
            allowed_choices=taxonomy(),
        )

        self.assertNotIn("research_packet_sha256", unenriched.as_dict())
        self.assertEqual(unenriched.fingerprint, built.fingerprint)

    def test_build_canonical_context_rejects_a_malformed_packet_hash(self):
        with self.assertRaises(ValueError):
            build_canonical_context(
                database="finance",
                merchant="example alpha cafe",
                amount=12.50,
                statement_category=None,
                allowed_choices=taxonomy(),
                research_packet_sha256="not-a-digest",
            )

    def test_tampered_fingerprint_is_rejected(self):
        raw = copy_case("select")
        raw["fingerprint"] = "0" * 64
        case = parse_one(raw)

        with self.assertRaises(ApprovalError) as raised:
            case.build_context(taxonomy())

        self.assertIn("packet-bound context", str(raised.exception))


class TestApprovedPacketEligibility(unittest.TestCase):
    def setUp(self) -> None:
        self._temporary = TemporaryDirectory()
        self.addCleanup(self._temporary.cleanup)
        self.root = Path(self._temporary.name)
        self.packets = {
            raw["normalized_merchant"]: build_packet(raw)
            for raw in load_fixture()["packets"]
        }

    def store_packets(self, *, approve: bool = True, skip: str | None = None) -> None:
        for merchant, packet in self.packets.items():
            if merchant == skip:
                continue
            store_packet(packet, root=self.root)
            if approve:
                record_review(
                    PacketReviewRecord(
                        packet_id=packet.packet_id,
                        packet_sha256=packet.packet_sha256,
                        status=PacketReviewStatus.APPROVED,
                        reviewed_at=utc_now(),
                    ),
                    root=self.root,
                )

    def resolver(self):
        return lambda merchant: resolve_approved_packet(merchant, root=self.root)

    def load(self, path: Path = FIXTURE):
        return load_enriched_gold_cases(
            "finance", taxonomy(), packet_resolver=self.resolver(), path=path
        )

    def reason_for(self, merchant: str) -> UnresolvedReason | None:
        resolution = resolve_approved_packet(merchant, root=self.root)
        return resolution.reason

    def test_approved_packets_satisfy_eligibility(self):
        self.store_packets()

        cases = self.load()

        self.assertEqual(len(cases), 4)
        for case in cases:
            self.assertIn(case.merchant, self.packets)

    def test_absent_packet_blocks_eligibility(self):
        self.store_packets(skip="example beta widgets")

        with self.assertRaises(ApprovalError) as raised:
            self.load()

        self.assertIn("no approved research packet", str(raised.exception))
        self.assertIs(
            self.reason_for("example beta widgets"), UnresolvedReason.RESEARCH_MISSING
        )

    def test_unapproved_packet_blocks_eligibility(self):
        self.store_packets(approve=False)

        with self.assertRaises(ApprovalError):
            self.load()

        # V47: a pending packet is typed unapproved, never silently usable.
        self.assertIs(
            self.reason_for("example alpha cafe"),
            UnresolvedReason.RESEARCH_UNAPPROVED,
        )

    def test_rejected_packet_blocks_eligibility(self):
        self.store_packets()
        packet = self.packets["example ambiguous counter"]
        record_review(
            PacketReviewRecord(
                packet_id=packet.packet_id,
                packet_sha256=packet.packet_sha256,
                status=PacketReviewStatus.REJECTED,
                reviewed_at=utc_now(),
                reason="Synthetic rejection for the fixture.",
            ),
            root=self.root,
        )

        with self.assertRaises(ApprovalError):
            self.load()

        self.assertIs(
            self.reason_for("example ambiguous counter"),
            UnresolvedReason.RESEARCH_UNAPPROVED,
        )

    def test_tampered_packet_blocks_eligibility(self):
        self.store_packets()
        packet = self.packets["example alpha cafe"]
        path = packet_path_for(packet.packet_id, root=self.root)
        document = json.loads(path.read_text(encoding="utf-8"))
        document["fetched_pages"][0]["text"] = "Tampered evidence text."
        path.write_text(json.dumps(document), encoding="utf-8")

        with self.assertRaises(ApprovalError):
            self.load()

        self.assertIs(
            self.reason_for("example alpha cafe"), UnresolvedReason.RESEARCH_TAMPERED
        )

    def test_stale_packet_hash_blocks_eligibility(self):
        self.store_packets()
        document = load_fixture()
        document["cases"][0]["research_packet_sha256"] = PACKET_HASH
        path = self.root / "stale-hash-gold.json"
        path.write_text(json.dumps(document), encoding="utf-8")

        with self.assertRaises(ApprovalError) as raised:
            self.load(path)

        self.assertIn("stale packet hash", str(raised.exception))

    def test_schema_version_drift_blocks_eligibility(self):
        self.store_packets()
        document = load_fixture()
        document["cases"][0]["packet_schema_version"] = "transaction-web-research-v0"
        path = self.root / "stale-schema-gold.json"
        path.write_text(json.dumps(document), encoding="utf-8")

        with self.assertRaises(ApprovalError) as raised:
            self.load(path)

        self.assertIn("schema version is stale", str(raised.exception))

    def test_query_version_drift_blocks_eligibility(self):
        self.store_packets()
        document = load_fixture()
        document["cases"][0]["packet_query_version"] = "merchant-research-v0"
        path = self.root / "stale-query-gold.json"
        path.write_text(json.dumps(document), encoding="utf-8")

        with self.assertRaises(ApprovalError) as raised:
            self.load(path)

        self.assertIn("query version is stale", str(raised.exception))

    def test_missing_gold_file_blocks_eligibility(self):
        self.store_packets()

        with self.assertRaises(ApprovalError):
            self.load(self.root / "absent-gold.json")

    def test_eligibility_is_checked_before_any_categorizer_exists(self):
        import inspect

        parameters = inspect.signature(load_enriched_gold_cases).parameters

        # No provider client, categorizer, or factory can be injected, so the
        # packet gate necessarily runs before any provider call (V31/V47).
        for forbidden in ("categorizer", "factory", "client", "config"):
            self.assertNotIn(forbidden, parameters)


class TestActionSpecificMatching(unittest.TestCase):
    def select_case(self):
        return parse_one(copy_case("select"))

    def suggestion_case(self):
        return parse_one(copy_case("suggest_new"))

    def abstain_case(self):
        return parse_one(copy_case("abstain"))

    def test_select_matches_only_its_exact_choice(self):
        case = self.select_case()

        self.assertTrue(
            case.matches(EnrichedSelect(101, (EVIDENCE_URL,), CONTEXT_FINGERPRINT))
        )
        self.assertFalse(
            case.matches(EnrichedSelect(102, (EVIDENCE_URL,), CONTEXT_FINGERPRINT))
        )
        self.assertFalse(case.matches(EnrichedAbstain("unclear", CONTEXT_FINGERPRINT)))

    def test_abstain_matches_only_abstain(self):
        case = self.abstain_case()

        self.assertTrue(case.matches(EnrichedAbstain("unclear", CONTEXT_FINGERPRINT)))
        self.assertFalse(
            case.matches(EnrichedSelect(101, (EVIDENCE_URL,), CONTEXT_FINGERPRINT))
        )

    def test_suggestion_matches_its_exact_proposal(self):
        case = self.suggestion_case()

        self.assertTrue(
            case.matches(
                EnrichedSuggestion(
                    "Sample Hobbies",
                    "Sample Widgets",
                    None,
                    "No offered choice covers widget vendors.",
                    (EVIDENCE_URL,),
                    CONTEXT_FINGERPRINT,
                )
            )
        )

    def test_suggestion_name_comparison_is_normalized(self):
        case = self.suggestion_case()

        self.assertTrue(
            case.matches(
                EnrichedSuggestion(
                    "  sample   hobbies ",
                    "SAMPLE WIDGETS",
                    None,
                    "Rationale text.",
                    (EVIDENCE_URL,),
                    CONTEXT_FINGERPRINT,
                )
            )
        )

    def test_suggestion_citations_are_an_unordered_evidence_set(self):
        case = self.suggestion_case()

        self.assertTrue(
            case.matches(
                EnrichedSuggestion(
                    "Sample Hobbies",
                    "Sample Widgets",
                    None,
                    "Rationale text.",
                    (EVIDENCE_URL,),
                    CONTEXT_FINGERPRINT,
                )
            )
        )

    def test_suggestion_rejects_a_different_evidence_set(self):
        case = self.suggestion_case()

        self.assertFalse(
            case.matches(
                EnrichedSuggestion(
                    "Sample Hobbies",
                    "Sample Widgets",
                    None,
                    "Rationale text.",
                    ("https://elsewhere.example/",),
                    CONTEXT_FINGERPRINT,
                )
            )
        )

    def test_suggestion_rejects_a_wrong_parent(self):
        case = self.suggestion_case()
        parented = parse_one(case_with_parent())

        decision = EnrichedSuggestion(
            "Sample Food",
            "Sample Snacks",
            11,
            "Rationale text.",
            ("https://gamma.example/",),
            CONTEXT_FINGERPRINT,
        )

        self.assertFalse(case.matches(decision))
        self.assertTrue(parented.matches(decision))

    def test_suggestion_rejects_a_wrong_subcategory(self):
        case = self.suggestion_case()

        self.assertFalse(
            case.matches(
                EnrichedSuggestion(
                    "Sample Hobbies",
                    "Sample Gadgets",
                    None,
                    "Rationale text.",
                    (EVIDENCE_URL,),
                    CONTEXT_FINGERPRINT,
                )
            )
        )

    def test_evaluator_reports_only_the_model_output(self):
        case = self.select_case()
        decision = EnrichedSelect(102, (EVIDENCE_URL,), CONTEXT_FINGERPRINT)

        result = evaluate_enriched_case(case, decision)

        self.assertFalse(result.matched)
        self.assertEqual(result.fingerprint, case.fingerprint)
        self.assertEqual(result.detail, "select:102")
        self.assertNotIn(str(case.expected_choice_id), result.detail)

    def test_evaluator_labels_each_action(self):
        self.assertEqual(
            describe_enriched_decision(
                EnrichedSelect(101, (EVIDENCE_URL,), CONTEXT_FINGERPRINT)
            ),
            "select:101",
        )
        self.assertEqual(
            describe_enriched_decision(
                EnrichedSuggestion(
                    "Sample Hobbies",
                    "Sample Widgets",
                    None,
                    "Rationale text.",
                    (EVIDENCE_URL,),
                    CONTEXT_FINGERPRINT,
                )
            ),
            "suggest_new:Sample Hobbies/Sample Widgets",
        )
        self.assertEqual(
            describe_enriched_decision(EnrichedAbstain("unclear", CONTEXT_FINGERPRINT)),
            "abstain:None",
        )
        self.assertEqual(describe_enriched_decision(object()), "invalid:object")


class TestZeroWebCalls(unittest.TestCase):
    def test_gold_path_never_imports_the_tinyfish_client(self):
        # A fresh interpreter is required: other test modules in the same
        # suite legitimately import the research CLI and therefore the TinyFish
        # client, which would pollute sys.modules here.
        script = (
            "import sys, services.gold_validator, services.transaction_llm_approval;"
            " assert 'services.tinyfish_research' not in sys.modules,"
            " 'the gold path must make zero TinyFish calls'"
        )

        completed = subprocess.run(
            [sys.executable, "-c", script],
            cwd=REPO_ROOT,
            capture_output=True,
            text=True,
            check=False,
        )

        self.assertEqual(completed.returncode, 0, completed.stderr)

    def test_gold_modules_contain_no_tinyfish_reference(self):
        for name in ("transaction_llm_approval.py", "gold_validator.py"):
            with self.subTest(module=name):
                source = (REPO_ROOT / "services" / name).read_text(encoding="utf-8")
                self.assertNotIn("tinyfish", source.lower())


if __name__ == "__main__":
    unittest.main()

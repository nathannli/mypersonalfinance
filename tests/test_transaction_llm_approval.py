import json
import os
import subprocess
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from typing import cast

from config import Config
from services.categorizer_factory import build_categorizer
from services.gold_validator import (
    EnrichedCaseResult,
    EnrichedPassResult,
    EnrichedValidationResult,
    production_categorizer_factory,
    run_pass,
    run_validation,
    write_approval_record,
    write_enriched_approval_record,
)
from services.llm_categorizer import (
    SYSTEM_PROMPT,
    OpenCodexCategorizer,
    build_request_bytes,
    build_request_payload,
    canonical_prompt_bytes,
    canonical_response_schema_bytes,
)
from services.transaction_categorization import (
    CategorizationResult,
    ProviderAction,
    UnresolvedReason,
)
from services.transaction_llm_approval import (
    REQUIRED_PASSES,
    ApprovalError,
    EnrichedGoldCase,
    authorize_enriched_write_mode,
    authorize_write_mode,
    build_enriched_fingerprints,
    build_fingerprints,
    build_write_authorizer,
    enriched_approval_key,
    enriched_gold_subset_hash,
    load_enriched_approval_record,
    parse_enriched_gold_cases,
    load_gold_cases,
    parse_gold_cases,
    sha256_hex,
)
from test_enriched_llm_categorizer import (
    LIVE_CHOICES,
    PARENTS_CHOICES,
    make_context,
    make_packet,
    parents_context,
)
from utils.repo_paths import private_approval_path, private_gold_path, repo_root

FIXTURE_PATH = (
    Path(__file__).resolve().parent
    / "fixtures"
    / ("transaction_llm_gold_synthetic.json")
)
FIXTURE = json.loads(FIXTURE_PATH.read_text(encoding="utf-8"))
TAXONOMIES = FIXTURE["taxonomies"]

BASE_URL = "http://localhost:10100"
MODEL = "exact-model-id"


def fixture_document() -> dict:
    return {"cases": [dict(case) for case in FIXTURE["cases"]]}


def write_json(directory: Path, name: str, document) -> Path:
    path = directory / name
    path.write_text(json.dumps(document), encoding="utf-8")
    return path


class FakeCategorizer:
    """Stands in for the production categorizer; makes no network call."""

    def __init__(self, responses, fail_on_call=None):
        self.responses = responses
        self.fail_on_call = fail_on_call
        self.provider_call_count = 0
        self.cache_hit_count = 0
        self.circuit_open = False
        self._cache = {}

    def categorize(self, context):
        if context.fingerprint in self._cache:
            self.cache_hit_count += 1
            return self._cache[context.fingerprint]
        self.provider_call_count += 1
        if self.fail_on_call == self.provider_call_count:
            self.circuit_open = True
            return CategorizationResult(
                ProviderAction.UNRESOLVED, reason=UnresolvedReason.TIMEOUT
            )
        result = self.responses[context.fingerprint]
        self._cache[context.fingerprint] = result
        return result


class RecordingFactory:
    """Production-shaped factory that records each fresh categorizer."""

    def __init__(self, responses, **kwargs):
        self.responses = responses
        self.kwargs = kwargs
        self.created: list[FakeCategorizer] = []

    def __call__(self) -> FakeCategorizer:
        categorizer = FakeCategorizer(self.responses, **self.kwargs)
        self.created.append(categorizer)
        return categorizer


def expected_responses(cases):
    responses = {}
    for case in cases:
        if case.expected_action == ProviderAction.SELECT:
            responses[case.fingerprint] = CategorizationResult(
                ProviderAction.SELECT, choice_id=case.expected_choice_id
            )
        else:
            responses[case.fingerprint] = CategorizationResult(
                ProviderAction.ABSTAIN, reason=UnresolvedReason.ABSTAINED
            )
    return responses


class TestPrivateArtifactPaths(unittest.TestCase):
    def test_paths_resolve_from_repository_root_not_cwd(self):
        expected_gold = repo_root() / ".transaction-llm-gold.json"
        original_cwd = Path.cwd()

        with tempfile.TemporaryDirectory() as tmp:
            os.chdir(tmp)
            try:
                self.assertEqual(private_gold_path(), expected_gold)
                self.assertEqual(
                    private_approval_path(),
                    repo_root() / ".transaction-llm-approval.json",
                )
            finally:
                os.chdir(original_cwd)

    def test_private_artifacts_are_git_ignored(self):
        ignore_text = (repo_root() / ".gitignore").read_text(encoding="utf-8")

        self.assertIn(".transaction-llm-gold.json", ignore_text)
        self.assertIn(".transaction-llm-approval.json", ignore_text)

    def test_private_artifacts_are_not_tracked_in_the_repository(self):
        tracked = subprocess.run(
            ["git", "ls-files"],
            cwd=repo_root(),
            capture_output=True,
            text=True,
            check=True,
        ).stdout.splitlines()
        self.assertNotIn(".transaction-llm-gold.json", tracked)
        self.assertNotIn(".transaction-llm-approval.json", tracked)


class TestGoldParsing(unittest.TestCase):
    def test_cases_are_filtered_by_database(self):
        finance = parse_gold_cases(fixture_document(), "finance")
        parents = parse_gold_cases(fixture_document(), "parents_finance")

        self.assertTrue(finance)
        self.assertTrue(parents)
        self.assertTrue(all(case.database == "finance" for case in finance))
        self.assertTrue(all(case.database == "parents_finance" for case in parents))

    def test_synthetic_fixture_covers_select_and_abstain_in_both_databases(self):
        for database in ("finance", "parents_finance"):
            with self.subTest(database=database):
                actions = {
                    case.expected_action
                    for case in parse_gold_cases(fixture_document(), database)
                }
                self.assertIn(ProviderAction.SELECT, actions)
                self.assertIn(ProviderAction.ABSTAIN, actions)

    def test_malformed_cases_are_rejected(self):
        bad_documents = {
            "not an object": [],
            "missing cases": {"nope": []},
            "unknown database": {"cases": [{"database": "other"}]},
            "missing fingerprint": {
                "cases": [{"database": "finance", "expected_action": "abstain"}]
            },
            "bad action": {
                "cases": [
                    {
                        "database": "finance",
                        "fingerprint": "a",
                        "expected_action": "guess",
                    }
                ]
            },
            "bool choice id": {
                "cases": [
                    {
                        "database": "finance",
                        "fingerprint": "a",
                        "expected_action": "select",
                        "expected_choice_id": True,
                        "merchant": "m",
                        "amount_minor_units": 1,
                    }
                ]
            },
            "abstain with choice": {
                "cases": [
                    {
                        "database": "finance",
                        "fingerprint": "a",
                        "expected_action": "abstain",
                        "expected_choice_id": 3,
                        "merchant": "m",
                        "amount_minor_units": 1,
                    }
                ]
            },
        }

        for label, document in bad_documents.items():
            with self.subTest(case=label):
                with self.assertRaises(ApprovalError):
                    parse_gold_cases(document, "finance")

    def test_duplicate_fingerprints_within_one_database_are_rejected(self):
        case = dict(FIXTURE["cases"][0])
        document = {"cases": [case, dict(case)]}

        with self.assertRaises(ApprovalError):
            parse_gold_cases(document, "finance")

    def test_missing_gold_file_is_an_approval_error(self):
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(ApprovalError):
                load_gold_cases("finance", Path(tmp) / "absent.json")

    def test_case_context_must_match_live_taxonomy(self):
        case = parse_gold_cases(fixture_document(), "finance")[0]
        drifted = [dict(choice) for choice in TAXONOMIES["finance"]]
        drifted[0]["subcategory_name"] = "Renamed Subcategory"

        with self.assertRaises(ApprovalError):
            case.build_context(drifted)


class TestWriteAuthorizer(unittest.TestCase):
    def setUp(self):
        self.cases = parse_gold_cases(fixture_document(), "finance")
        self.authorize = build_write_authorizer(self.cases)
        self.select_case = next(
            case for case in self.cases if case.expected_action == ProviderAction.SELECT
        )
        self.abstain_case = next(
            case
            for case in self.cases
            if case.expected_action == ProviderAction.ABSTAIN
        )

    def test_approved_context_with_approved_choice_is_authorized(self):
        context = self.select_case.build_context(TAXONOMIES["finance"])

        self.assertTrue(self.authorize(context, self.select_case.expected_choice_id))

    def test_approved_context_with_other_choice_is_refused(self):
        context = self.select_case.build_context(TAXONOMIES["finance"])
        other = self.select_case.expected_choice_id + 1

        self.assertFalse(self.authorize(context, other))

    def test_expected_abstain_case_never_authorizes_a_write(self):
        context = self.abstain_case.build_context(TAXONOMIES["finance"])

        self.assertFalse(self.authorize(context, 101))

    def test_unseen_context_is_refused(self):
        parents_case = parse_gold_cases(fixture_document(), "parents_finance")[0]
        context = parents_case.build_context(TAXONOMIES["parents_finance"])

        self.assertFalse(self.authorize(context, parents_case.expected_choice_id))


class TestApprovalRecordGate(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.directory = Path(self.tmp.name)
        self.addCleanup(self.tmp.cleanup)
        self.gold_path = write_json(self.directory, "gold.json", fixture_document())
        self.cases = parse_gold_cases(fixture_document(), "finance")

    def approval_document(self, database="finance", **overrides):
        record = build_fingerprints(
            database, BASE_URL, MODEL, TAXONOMIES[database], self.cases
        )
        record["passes"] = REQUIRED_PASSES
        record["approved_at"] = "2026-09-15T00:00:00+00:00"
        record.update(overrides)
        return {database: record}

    def authorize(self, document, database="finance"):
        approval_path = write_json(self.directory, "approval.json", document)
        return authorize_write_mode(
            database=database,
            base_url=BASE_URL,
            model=MODEL,
            choices=TAXONOMIES[database],
            gold_path=self.gold_path,
            approval_path=approval_path,
        )

    def test_matching_record_returns_working_authorizer(self):
        authorize = self.authorize(self.approval_document())
        select_case = next(
            case for case in self.cases if case.expected_action == ProviderAction.SELECT
        )
        context = select_case.build_context(TAXONOMIES["finance"])

        self.assertTrue(authorize(context, select_case.expected_choice_id))

    def test_missing_approval_file_aborts(self):
        with self.assertRaises(ApprovalError):
            authorize_write_mode(
                database="finance",
                base_url=BASE_URL,
                model=MODEL,
                choices=TAXONOMIES["finance"],
                gold_path=self.gold_path,
                approval_path=self.directory / "absent.json",
            )

    def test_stale_identity_fields_abort(self):
        stale_variants = {
            "model": {"model": "other-model"},
            "base_url": {"base_url": "http://localhost:19999"},
            "prompt": {"prompt_sha256": sha256_hex(b"different prompt")},
            "schema": {"schema_sha256": sha256_hex(b"different schema")},
            "taxonomy": {"taxonomy_sha256": sha256_hex(b"different taxonomy")},
            "gold": {"gold_sha256": sha256_hex(b"different gold")},
        }

        for label, override in stale_variants.items():
            with self.subTest(field=label):
                with self.assertRaises(ApprovalError):
                    self.authorize(self.approval_document(**override))

    def test_changed_live_taxonomy_aborts(self):
        drifted = [dict(choice) for choice in TAXONOMIES["finance"]]
        drifted[0]["category_name"] = "Renamed Category"
        approval_path = write_json(
            self.directory, "approval.json", self.approval_document()
        )

        with self.assertRaises(ApprovalError):
            authorize_write_mode(
                database="finance",
                base_url=BASE_URL,
                model=MODEL,
                choices=drifted,
                gold_path=self.gold_path,
                approval_path=approval_path,
            )

    def test_fewer_than_three_passes_aborts(self):
        with self.assertRaises(ApprovalError):
            self.authorize(self.approval_document(passes=2))

    def test_finance_approval_cannot_authorize_parents_finance(self):
        with self.assertRaises(ApprovalError):
            self.authorize(
                self.approval_document("finance"), database="parents_finance"
            )

    def test_prompt_and_schema_hashes_track_production_bytes(self):
        record = self.approval_document()["finance"]

        self.assertEqual(record["prompt_sha256"], sha256_hex(canonical_prompt_bytes()))
        self.assertEqual(
            record["schema_sha256"], sha256_hex(canonical_response_schema_bytes())
        )


class TestFinanceEnrichedApprovalIsPinned(unittest.TestCase):
    """V1: literals from before parents_finance joined the enriched path."""

    def setUp(self):
        packet = make_packet()
        context = replace(
            make_context(),
            research_packet_sha256=packet.packet_sha256,
            research_packet_schema_version=packet.schema_version,
            research_packet_query_version=packet.query_version,
        )
        self.case = EnrichedGoldCase(
            database="finance",
            fingerprint=context.fingerprint,
            merchant=context.merchant,
            amount_minor_units=context.amount_minor_units,
            statement_category=None,
            research_packet_sha256=packet.packet_sha256,
            packet_schema_version=packet.schema_version,
            packet_query_version=packet.query_version,
            expected_action="select",
            expected_choice_id=11,
        )

    def test_fingerprints_and_key(self):
        self.assertEqual(
            build_enriched_fingerprints(
                "finance", BASE_URL, MODEL, LIVE_CHOICES, [self.case]
            ),
            {
                "base_url": "http://localhost:10100",
                "database": "finance",
                "gold_sha256": "8de3d3a77a4a7ec52201fca65efbfcfb8cc31ba6b5665bef2394e8c1dfab122a",
                "mode": "write",
                "model": "exact-model-id",
                "packet_identity_sha256": "17109dbeabab93029075a5b07aa3f9c500aed01ea4bcd299bda7960708137aaf",
                "prompt_sha256": "14a6e9f329d50082bf785c3edb4078849e527c25b3505d8535262f90002c9a4f",
                "protocol": "enriched",
                "schema_sha256": "49b55f31938de74d7eeefc0d9f7110acbb8653b84828de7a46e59dd72aa26a3a",
                "taxonomy_sha256": "0bda3691344dfa35460404e2aff8f4425c1c36323244044f0d451c705bdd98db",
            },
        )
        self.assertEqual(
            enriched_approval_key(BASE_URL, MODEL),
            "finance:enriched:d8f2ce312974b1923db5d7928a827ce1d4158152d79936d6b09cf0c68761b7f4",
        )

    def test_writer_keeps_bare_key_and_migrates_the_previous_record(self):
        passed = EnrichedPassResult(
            results=[EnrichedCaseResult(self.case.fingerprint, True, "select:11")]
        )
        previous = {"base_url": BASE_URL, "model": "older-model"}
        with tempfile.TemporaryDirectory() as directory:
            path = write_json(
                Path(directory), "approval.json", {"finance:enriched": previous}
            )
            write_enriched_approval_record(
                "finance",
                BASE_URL,
                MODEL,
                LIVE_CHOICES,
                [self.case],
                EnrichedValidationResult([passed] * REQUIRED_PASSES),
                approval_path=path,
            )
            document = json.loads(path.read_text(encoding="utf-8"))

        current = enriched_approval_key(BASE_URL, MODEL)
        migrated = enriched_approval_key(BASE_URL, "older-model")
        self.assertEqual(set(document), {"finance:enriched", current, migrated})
        self.assertEqual(document[migrated], previous)
        self.assertEqual(document["finance:enriched"], document[current])
        self.assertEqual(document[current]["model"], MODEL)


class TestParentsEnrichedApproval(unittest.TestCase):
    """V6/V7/V8: parents approval lives only under its own per-model key."""

    database = "parents_finance"

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.directory = Path(self.tmp.name)
        self.packet = make_packet()
        self.context = replace(
            parents_context(),
            research_packet_sha256=self.packet.packet_sha256,
            research_packet_schema_version=self.packet.schema_version,
            research_packet_query_version=self.packet.query_version,
        )
        self.case = EnrichedGoldCase(
            database=self.database,
            fingerprint=self.context.fingerprint,
            merchant=self.context.merchant,
            amount_minor_units=self.context.amount_minor_units,
            statement_category=None,
            research_packet_sha256=self.packet.packet_sha256,
            packet_schema_version=self.packet.schema_version,
            packet_query_version=self.packet.query_version,
            expected_action="select",
            expected_choice_id=2,
        )
        self.gold_path = write_json(
            self.directory, "gold.json", {"cases": [self.case.as_payload()]}
        )
        self.key = enriched_approval_key(BASE_URL, MODEL, self.database)

    def passed(self):
        single = EnrichedPassResult(
            results=[EnrichedCaseResult(self.case.fingerprint, True, "select:2")]
        )
        return EnrichedValidationResult([single] * REQUIRED_PASSES)

    def write(self, path):
        return write_enriched_approval_record(
            self.database,
            BASE_URL,
            MODEL,
            PARENTS_CHOICES,
            [self.case],
            self.passed(),
            approval_path=path,
        )

    def authorize(self, path):
        return authorize_enriched_write_mode(
            self.database,
            BASE_URL,
            MODEL,
            lambda: PARENTS_CHOICES,
            packet_resolver=lambda merchant: SimpleNamespace(packet=self.packet),
            gold_path=self.gold_path,
            approval_path=path,
        )

    def test_key_differs_only_by_the_database_prefix(self):
        finance = enriched_approval_key(BASE_URL, MODEL)

        self.assertEqual(self.key, f"parents_finance:{finance.split(':', 1)[1]}")

    def test_writing_adds_one_key_and_leaves_finance_records_alone(self):
        finance = {
            "finance:enriched": {"base_url": BASE_URL, "model": "older-model"},
            enriched_approval_key(BASE_URL, MODEL): {"model": MODEL},
        }
        path = write_json(self.directory, "approval.json", finance)

        self.write(path)
        document = json.loads(path.read_text(encoding="utf-8"))

        self.assertEqual(set(document), {*finance, self.key})
        for key, record in finance.items():
            self.assertEqual(document[key], record)

    def test_written_approval_authorizes_only_the_approved_choice(self):
        path = self.directory / "approval.json"
        self.write(path)

        authorize = self.authorize(path)

        self.assertTrue(authorize(self.context, self.packet, 2))
        self.assertFalse(authorize(self.context, self.packet, 1))

    def test_no_other_record_is_ever_used(self):
        source = self.directory / "source.json"
        self.write(source)
        record = json.loads(source.read_text(encoding="utf-8"))[self.key]
        for key in (
            "finance:enriched",
            "parents_finance:enriched",
            enriched_approval_key(BASE_URL, MODEL),
        ):
            with self.subTest(key=key):
                path = write_json(self.directory, "approval.json", {key: record})
                with self.assertRaisesRegex(ApprovalError, "no entry"):
                    self.authorize(path)

    def test_a_parents_record_never_authorizes_finance(self):
        path = self.directory / "approval.json"
        self.write(path)

        with self.assertRaisesRegex(ApprovalError, "no entry"):
            load_enriched_approval_record(
                path, key=enriched_approval_key(BASE_URL, MODEL)
            )

    def test_model_case_fingerprints_are_mandatory(self):
        path = self.directory / "approval.json"
        self.write(path)
        document = json.loads(path.read_text(encoding="utf-8"))
        for value in (None, []):
            with self.subTest(value=value):
                changed = json.loads(json.dumps(document))
                if value is None:
                    del changed[self.key]["model_case_fingerprints"]
                else:
                    changed[self.key]["model_case_fingerprints"] = value
                write_json(self.directory, "approval.json", changed)
                with self.assertRaisesRegex(ApprovalError, "fingerprints"):
                    self.authorize(path)

    def test_finance_gold_changes_leave_parents_identity_alone(self):
        document = {"cases": [self.case.as_payload()]}
        before = enriched_gold_subset_hash(
            parse_enriched_gold_cases(document, self.database)
        )
        finance = json.loads(
            (
                FIXTURE_PATH.parent / "transaction_llm_gold_enriched_synthetic.json"
            ).read_text(encoding="utf-8")
        )
        document["cases"] += finance["cases"]

        self.assertEqual(
            enriched_gold_subset_hash(
                parse_enriched_gold_cases(document, self.database)
            ),
            before,
        )

    def test_legacy_validation_and_authorizer_refuse_parents(self):
        path = write_json(self.directory, "legacy.json", fixture_document())
        for call in (
            lambda: load_gold_cases(self.database, path),
            lambda: authorize_write_mode(
                self.database, BASE_URL, MODEL, PARENTS_CHOICES, gold_path=path
            ),
        ):
            with self.assertRaisesRegex(ApprovalError, "--enriched"):
                call()


class TestGoldValidator(unittest.TestCase):
    def setUp(self):
        self.cases = parse_gold_cases(fixture_document(), "finance")
        self.choices = TAXONOMIES["finance"]
        self.responses = expected_responses(self.cases)

    def factory(self, **kwargs):
        return RecordingFactory(self.responses, **kwargs)

    def test_each_pass_uses_a_fresh_categorizer_and_one_call_per_case(self):
        factory = self.factory()

        validation = run_validation(self.cases, self.choices, factory)

        self.assertTrue(validation.approved)
        self.assertEqual(len(validation.passes), REQUIRED_PASSES)
        self.assertEqual(len(factory.created), REQUIRED_PASSES)
        for single in validation.passes:
            self.assertEqual(single.provider_calls, len(self.cases))

    def test_reused_categorizer_cache_fails_the_pass(self):
        shared = FakeCategorizer(self.responses)

        first = run_pass(self.cases, self.choices, lambda: shared)
        self.assertTrue(first.passed)

        with self.assertRaises(ApprovalError):
            run_pass(self.cases, self.choices, lambda: shared)

    def test_mismatched_choice_fails_the_pass_and_stops_validation(self):
        select_case = next(
            case for case in self.cases if case.expected_action == ProviderAction.SELECT
        )
        self.responses[select_case.fingerprint] = CategorizationResult(
            ProviderAction.SELECT, choice_id=select_case.expected_choice_id + 1
        )

        validation = run_validation(self.cases, self.choices, self.factory())

        self.assertFalse(validation.approved)
        self.assertEqual(len(validation.passes), 1)

    def test_open_circuit_fails_the_pass(self):
        validation = run_validation(
            self.cases, self.choices, self.factory(fail_on_call=1)
        )

        self.assertFalse(validation.approved)
        self.assertFalse(validation.passes[0].passed)

    def test_empty_subset_is_rejected(self):
        with self.assertRaises(ApprovalError):
            run_validation([], self.choices, self.factory())


class TestValidatorRuntimeParity(unittest.TestCase):
    """The validator must exercise the exact production request path."""

    @staticmethod
    def config() -> Config:
        return cast(
            Config,
            SimpleNamespace(
                llm_base_url=BASE_URL,
                llm_api_key="x",
                transaction_llm_model=MODEL,
                enriched_transaction_llm_model=MODEL,
                transaction_llm_timeout_seconds=5.0,
                transaction_llm_mode="shadow",
            ),
        )

    def test_validator_factory_builds_production_categorizer(self):
        factory = production_categorizer_factory(self.config())

        first = factory()
        second = factory()

        self.assertIsInstance(first, OpenCodexCategorizer)
        self.assertIsNot(first, second)
        self.assertEqual(first.config.model, MODEL)
        self.assertEqual(first.provider_call_count, 0)
        self.assertEqual(first.cache_hit_count, 0)
        self.assertFalse(first.circuit_open)

    def test_validator_context_produces_identical_runtime_request_bytes(self):
        case = parse_gold_cases(fixture_document(), "finance")[0]
        validator_context = case.build_context(TAXONOMIES["finance"])
        runtime_context = case.build_context(list(TAXONOMIES["finance"]))

        self.assertEqual(
            build_request_bytes(validator_context, MODEL),
            build_request_bytes(runtime_context, MODEL),
        )
        self.assertEqual(validator_context.fingerprint, runtime_context.fingerprint)

    def test_runtime_and_validator_share_one_categorizer_implementation(self):
        config = self.config()

        runtime = build_categorizer(config)
        validated = production_categorizer_factory(config)()

        self.assertIs(type(runtime), type(validated))
        self.assertEqual(runtime.config, validated.config)


class TestPromptInjectionHandling(unittest.TestCase):
    def setUp(self):
        self.case = next(
            case
            for case in parse_gold_cases(fixture_document(), "finance")
            if "ignore previous instructions" in case.merchant
        )
        self.context = self.case.build_context(TAXONOMIES["finance"])

    def test_adversarial_merchant_is_carried_as_data_not_instructions(self):
        payload = build_request_payload(self.context, MODEL)
        system_message, user_message = payload["messages"]

        self.assertEqual(system_message["content"], SYSTEM_PROMPT)
        self.assertNotIn(self.case.merchant, system_message["content"])
        self.assertEqual(
            json.loads(user_message["content"])["merchant"], self.case.merchant
        )

    def test_adversarial_gold_case_expects_abstain_and_never_authorizes_write(self):
        authorize = build_write_authorizer(
            parse_gold_cases(fixture_document(), "finance")
        )

        self.assertEqual(self.case.expected_action, ProviderAction.ABSTAIN)
        for choice in TAXONOMIES["finance"]:
            self.assertFalse(authorize(self.context, choice["subcategory_id"]))

    def test_both_databases_include_an_adversarial_case(self):
        for database in ("finance", "parents_finance"):
            with self.subTest(database=database):
                merchants = [
                    case.merchant
                    for case in parse_gold_cases(fixture_document(), database)
                ]
                self.assertTrue(
                    any(
                        "ignore previous instructions" in merchant
                        or "system:" in merchant
                        for merchant in merchants
                    )
                )


class TestApprovalRecordWriting(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / "approval.json"
        self.cases = parse_gold_cases(fixture_document(), "finance")
        self.choices = TAXONOMIES["finance"]
        self.responses = expected_responses(self.cases)

    def run_validation(self, **kwargs):
        return run_validation(
            self.cases,
            self.choices,
            lambda: FakeCategorizer(self.responses, **kwargs),
        )

    def test_record_is_written_only_after_three_passing_runs(self):
        validation = self.run_validation()

        write_approval_record(
            "finance", BASE_URL, MODEL, self.choices, self.cases, validation, self.path
        )
        document = json.loads(self.path.read_text(encoding="utf-8"))

        self.assertEqual(document["finance"]["passes"], REQUIRED_PASSES)

    def test_failed_validation_never_writes_a_record(self):
        validation = self.run_validation(fail_on_call=1)

        with self.assertRaises(ApprovalError):
            write_approval_record(
                "finance",
                BASE_URL,
                MODEL,
                self.choices,
                self.cases,
                validation,
                self.path,
            )

        self.assertFalse(self.path.exists())

    def test_record_contains_no_private_transaction_data(self):
        validation = self.run_validation()
        write_approval_record(
            "finance", BASE_URL, MODEL, self.choices, self.cases, validation, self.path
        )

        text = self.path.read_text(encoding="utf-8")

        for case in self.cases:
            self.assertNotIn(case.merchant, text)
        self.assertNotIn("amount_minor_units", text)
        self.assertNotIn("expected_choice_id", text)
        self.assertNotIn("api_key", text)

    def test_writing_one_database_preserves_the_other_record(self):
        self.path.write_text(
            json.dumps({"parents_finance": {"passes": 3}}), encoding="utf-8"
        )
        validation = self.run_validation()

        write_approval_record(
            "finance", BASE_URL, MODEL, self.choices, self.cases, validation, self.path
        )
        document = json.loads(self.path.read_text(encoding="utf-8"))

        self.assertIn("parents_finance", document)
        self.assertIn("finance", document)


if __name__ == "__main__":
    unittest.main()

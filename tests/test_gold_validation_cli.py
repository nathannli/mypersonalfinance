"""T13 tests: the production gold-validation entry point.

Network-free. Drives validate-transaction-llm-gold.py main() with the
real run_validation/write_approval_record logic and fake categorizers,
using the tracked synthetic fixture only (V10, V30).
"""

import importlib.util
import io
import json
import tempfile
import unittest
from contextlib import redirect_stdout
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from services.gold_validator import (
    EnrichedPassResult,
    EnrichedValidationResult,
    write_approval_record as real_write_approval_record,
)
from services.llm_categorizer import EnrichedExecution, EnrichedSelect
from services.research_packets import (
    FetchedPage,
    PacketStatus,
    ResearchPacket,
    SearchResult,
)
from services.transaction_categorization import (
    CategorizationResult,
    ProviderAction,
    build_canonical_context,
)
from services.transaction_llm_approval import (
    REQUIRED_PASSES,
    ApprovalError,
    EnrichedGoldCase,
    parse_gold_cases,
)

REPO_ROOT = Path(__file__).resolve().parents[1]
MODULE_PATH = REPO_ROOT / "validate-transaction-llm-gold.py"
FIXTURE = json.loads(
    (
        REPO_ROOT / "tests" / "fixtures" / "transaction_llm_gold_synthetic.json"
    ).read_text(encoding="utf-8")
)


def load_runner():
    spec = importlib.util.spec_from_file_location(
        "validate_transaction_llm_gold", MODULE_PATH
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


runner = load_runner()


def fake_config():
    return SimpleNamespace(
        llm_base_url="http://localhost:10100",
        llm_api_key="x",
        transaction_llm_model="exact-model-id",
        enriched_transaction_llm_model="enriched-model-id",
        transaction_llm_timeout_seconds=5.0,
        transaction_llm_mode="shadow",
    )


class SelectingCategorizer:
    """Fresh-per-pass fake returning each case's expected result."""

    def __init__(self, expectations):
        self.expectations = expectations
        self.provider_call_count = 0
        self.cache_hit_count = 0
        self.circuit_open = False

    def categorize(self, context):
        self.provider_call_count += 1
        action, choice_id = self.expectations[context.fingerprint]
        return CategorizationResult(
            action=action,
            choice_id=choice_id,
            context_fingerprint=context.fingerprint,
        )


def expectations_from(cases):
    table = {}
    for case in cases:
        if case.expected_action == ProviderAction.SELECT:
            table[case.fingerprint] = (
                ProviderAction.SELECT,
                case.expected_choice_id,
            )
        else:
            table[case.fingerprint] = (ProviderAction.ABSTAIN, None)
    return table


class RunnerTestCase(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.tmp_path = Path(tmp.name)
        self.approval_path = self.tmp_path / ".transaction-llm-approval.json"

    def gold_cases(self, database):
        return parse_gold_cases({"cases": FIXTURE["cases"]}, database)

    def wire(self, database, wrong_choice=False):
        cases = self.gold_cases(database)
        expectations = expectations_from(cases)
        if wrong_choice:
            for case in cases:
                if case.expected_action == ProviderAction.SELECT:
                    expectations[case.fingerprint] = (
                        ProviderAction.SELECT,
                        case.expected_choice_id + 1,
                    )
                    break

        factory_calls = []
        factory_models = []

        def factory(config, *, model=None):
            factory_models.append(model)

            def build():
                categorizer = SelectingCategorizer(dict(expectations))
                factory_calls.append(categorizer)
                return categorizer

            return build

        written = {}

        def write_record(**kwargs):
            path = real_write_approval_record(
                approval_path=self.approval_path, **kwargs
            )
            written[kwargs["database"]] = path
            return path

        taxonomy_rows = list(FIXTURE["taxonomies"][database])

        class FakeDB:
            def __init__(self, debug=False):
                pass

            def get_categorization_choices(self):
                return taxonomy_rows

        return SimpleNamespace(
            factory_calls=factory_calls,
            factory_models=factory_models,
            written=written,
            write_record=write_record,
            factory=factory,
            fake_db_class=FakeDB,
            cases=cases,
        )

    def run_main(self, database, wiring):
        argv = ["--database", database]
        with (
            patch.object(runner, "Config", fake_config),
            patch.object(runner, "DATABASE_CLASSES", {database: wiring.fake_db_class}),
            patch.object(runner, "load_gold_cases", lambda db: self.gold_cases(db)),
            patch.object(runner, "production_categorizer_factory", wiring.factory),
            patch.object(runner, "write_approval_record", wiring.write_record),
        ):
            buffer = io.StringIO()
            with redirect_stdout(buffer):
                code = runner.main(argv)
        return code, buffer.getvalue()


class TestApprovedRun(RunnerTestCase):
    def test_three_fresh_passes_write_approval_and_exit_zero(self):
        wiring = self.wire("finance")

        code, output = self.run_main("finance", wiring)

        self.assertEqual(code, 0)
        self.assertIn("Approved", output)
        self.assertEqual(len(wiring.factory_calls), 3)
        for categorizer in wiring.factory_calls:
            self.assertEqual(categorizer.provider_call_count, 4)
        self.assertEqual(sorted(wiring.written), ["finance"])
        self.assertTrue(wiring.written["finance"].exists())

    def test_approval_document_holds_only_selected_database(self):
        wiring = self.wire("finance")

        code, _ = self.run_main("finance", wiring)

        self.assertEqual(code, 0)
        document = json.loads(self.approval_path.read_text(encoding="utf-8"))
        self.assertEqual(sorted(document), ["finance"])


class TestFailedRun(RunnerTestCase):
    def test_mismatch_exits_one_and_writes_nothing(self):
        wiring = self.wire("finance", wrong_choice=True)

        code, output = self.run_main("finance", wiring)

        self.assertEqual(code, 1)
        self.assertIn("MISMATCH", output)
        self.assertEqual(wiring.written, {})
        self.assertFalse(self.approval_path.exists())

    def test_missing_gold_set_exits_one_without_write(self):
        wiring = self.wire("finance")

        def missing_gold(database):
            raise ApprovalError("private gold set is missing")

        with (
            patch.object(runner, "Config", fake_config),
            patch.object(runner, "DATABASE_CLASSES", {"finance": wiring.fake_db_class}),
            patch.object(runner, "load_gold_cases", missing_gold),
        ):
            buffer = io.StringIO()
            with redirect_stdout(buffer):
                code = runner.main(["--database", "finance"])

        self.assertEqual(code, 1)
        self.assertIn("Validation failed", buffer.getvalue())
        self.assertEqual(wiring.written, {})


class TestModelSelection(RunnerTestCase):
    """V51: each protocol validates with the exact model its path calls."""

    def test_legacy_run_validates_with_the_unenriched_model(self):
        wiring = self.wire("finance")

        code, _ = self.run_main("finance", wiring)

        self.assertEqual(code, 0)
        # The factory is resolved once per run; one fresh categorizer per pass.
        self.assertEqual(wiring.factory_models, ["exact-model-id"])
        self.assertEqual(len(wiring.factory_calls), 3)

    def test_enriched_run_validates_with_the_enriched_model(self):
        cases = [SimpleNamespace(amount_minor_units=900)]
        written = {}
        models = []

        def factory(config, *, model=None):
            models.append(model)
            return lambda: None

        def write_enriched(**kwargs):
            written.update(kwargs)
            return self.approval_path

        approved = EnrichedValidationResult(
            passes=[EnrichedPassResult(results=[]) for _ in range(3)]
        )

        class FakeDB:
            def __init__(self, debug=False):
                pass

            def get_categorization_choices(self):
                return FIXTURE["taxonomies"]["finance"]

            def select(self, query, args):
                return []

        with (
            patch.object(runner, "Config", fake_config),
            patch.object(runner, "DATABASE_CLASSES", {"finance": FakeDB}),
            patch.object(runner, "load_enriched_gold_cases", lambda *a, **k: cases),
            patch.object(runner, "run_enriched_validation", lambda *a, **k: approved),
            patch.object(runner, "production_categorizer_factory", factory),
            patch.object(runner, "write_enriched_approval_record", write_enriched),
        ):
            buffer = io.StringIO()
            with redirect_stdout(buffer):
                code = runner.main(["--database", "finance", "--enriched"])

        self.assertEqual(code, 0)
        # Both the provider factory and the approval record bind the enriched
        # model, never TRANSACTION_LLM_MODEL.
        self.assertEqual(models, ["enriched-model-id"])
        self.assertEqual(written["model"], "enriched-model-id")
        self.assertEqual(written["database"], "finance")

    def test_legacy_parents_validation_is_refused_naming_enriched(self):
        wiring = self.wire("parents_finance")

        with (
            patch.object(runner, "Config", fake_config),
            patch.object(
                runner, "DATABASE_CLASSES", {"parents_finance": wiring.fake_db_class}
            ),
        ):
            buffer = io.StringIO()
            with redirect_stdout(buffer):
                code = runner.main(["--database", "parents_finance"])

        self.assertEqual(code, 1)
        self.assertIn("--enriched", buffer.getvalue())
        self.assertEqual(wiring.factory_calls, [])


PARENTS_CHOICES = [
    {"category_id": 1, "category_name": "Groceries"},
    {"category_id": 2, "category_name": "Cash"},
]


def parents_packet(merchant):
    return ResearchPacket(
        normalized_merchant=merchant,
        derived_query="synthetic",
        status=PacketStatus.COMPLETE,
        searched_at="2026-09-17T12:00:00+00:00",
        search_results=(
            SearchResult(
                position=0,
                site_name="Acme",
                title="Acme",
                snippet="s",
                url="https://acme.example/",
            ),
        ),
        fetched_pages=(
            FetchedPage(
                url="https://acme.example/",
                final_url="https://acme.example/",
                title="Acme",
                description="d",
                text="t",
                relevance_matched_tokens=("acme",),
            ),
        ),
    )


def parents_case(merchant, choice_id, statement_category=None):
    packet = parents_packet(merchant)
    context = build_canonical_context(
        database="parents_finance",
        merchant=merchant,
        amount="10.00",
        statement_category=statement_category,
        allowed_choices=PARENTS_CHOICES,
        research_packet_sha256=packet.packet_sha256,
        research_packet_schema_version=packet.schema_version,
        research_packet_query_version=packet.query_version,
    )
    return EnrichedGoldCase(
        database="parents_finance",
        fingerprint=context.fingerprint,
        merchant=context.merchant,
        amount_minor_units=1000,
        statement_category=context.statement_category,
        research_packet_sha256=packet.packet_sha256,
        packet_schema_version=packet.schema_version,
        packet_query_version=packet.query_version,
        expected_action="select",
        expected_choice_id=choice_id,
    )


class ParentsReplayDB:
    """Rules are keyed by the exact raw spelling, as in the live table."""

    expenses: list = []
    exact: dict = {}
    substrings: dict = {}

    def __init__(self, debug=False):
        pass

    def get_categorization_choices(self):
        return PARENTS_CHOICES

    def select(self, query, args=None):
        if "auto_match" in query:
            return [(name,) for name in self.exact]
        return [(merchant, Decimal("10.00")) for merchant in self.expenses]

    def get_auto_match_category(self, merchant):
        return self.exact.get(merchant) or self.get_substring_auto_match_category(
            merchant
        )

    def get_substring_auto_match_category(self, merchant):
        matches = [
            category
            for pattern, category in self.substrings.items()
            if pattern in merchant.lower()
        ]
        if len(matches) > 1:
            raise ValueError("ambiguous")
        return matches[0] if matches else None


class TestParentsEnrichedValidation(unittest.TestCase):
    """V2/V3/V14: replay classifies like the loader; held rows stay local."""

    def run_main(self, case, *, expenses=(), exact=None, substrings=None):
        database = type(
            "DB",
            (ParentsReplayDB,),
            {
                "expenses": list(expenses),
                "exact": exact or {},
                "substrings": substrings or {},
            },
        )
        calls = []
        written = {}

        class Categorizer:
            provider_call_count = 0
            cache_hit_count = 0
            circuit_open = False

            def categorize_enriched(self, context, packet):
                self.provider_call_count += 1
                calls.append(context.merchant)
                return EnrichedExecution(
                    decision=EnrichedSelect(
                        case.expected_choice_id,
                        ("https://acme.example/",),
                        context.fingerprint,
                    )
                )

        def resolver(merchant, *, database):
            self.assertEqual(database, "parents_finance")
            return SimpleNamespace(packet=parents_packet(merchant))

        with (
            patch.object(runner, "Config", fake_config),
            patch.object(runner, "DATABASE_CLASSES", {"parents_finance": database}),
            patch.object(runner, "load_enriched_gold_cases", lambda *a, **k: [case]),
            patch.object(runner, "resolve_approved_packet", resolver),
            patch.object(
                runner,
                "production_categorizer_factory",
                lambda config, model=None: Categorizer,
            ),
            patch.object(
                runner,
                "write_enriched_approval_record",
                lambda **kwargs: written.update(kwargs) or Path("approval.json"),
            ),
        ):
            buffer = io.StringIO()
            with redirect_stdout(buffer):
                code = runner.main(["--database", "parents_finance", "--enriched"])
        return code, buffer.getvalue(), calls, written

    def test_unmatched_case_goes_to_the_model_three_times(self):
        code, _, calls, written = self.run_main(parents_case("acme widgets", 1))

        self.assertEqual(code, 0)
        self.assertEqual(len(calls), 3)
        self.assertEqual(written["database"], "parents_finance")
        self.assertEqual(written["model"], "enriched-model-id")

    def test_held_cases_fail_without_a_provider_call(self):
        for merchant in ("cad draft jane example", "td atm w/d 004"):
            with self.subTest(merchant=merchant):
                code, output, calls, written = self.run_main(parents_case(merchant, 1))

                self.assertEqual(code, 1)
                self.assertIn("manual", output)
                self.assertEqual((calls, written), ([], {}))

    def test_a_rule_matched_cash_case_is_checked_without_the_model(self):
        code, output, calls, _ = self.run_main(
            parents_case("td atm w/d 004", 2),
            expenses=["TD ATM W/D 004"],
            exact={"TD ATM W/D 004": "Cash"},
        )

        self.assertIn("deterministic:2 ok", output)
        self.assertEqual(calls, [])
        # No case passed through the model, so the writer decides; here faked.
        self.assertEqual(code, 0)

    def test_statement_category_replays_first(self):
        _, output, calls, _ = self.run_main(
            parents_case("acme widgets", 2, statement_category="Cash")
        )

        self.assertIn("deterministic:2 ok", output)
        self.assertEqual(calls, [])

    def test_ambiguous_spelling_leaves_only_substring_rules(self):
        case = parents_case("acme widgets", 2)
        _, output, calls, _ = self.run_main(
            case,
            expenses=["ACME WIDGETS", "Acme  Widgets"],
            exact={"ACME WIDGETS": "Groceries"},
            substrings={"acme": "Cash"},
        )

        self.assertIn("deterministic:2 ok", output)
        self.assertEqual(calls, [])

    def test_a_rule_spelling_is_recovered_from_auto_match(self):
        _, output, calls, _ = self.run_main(
            parents_case("acme widgets", 1), exact={"ACME WIDGETS": "Groceries"}
        )

        self.assertIn("deterministic:1 ok", output)
        self.assertEqual(calls, [])

    def test_invalid_mapping_fails_the_pass(self):
        code, output, calls, written = self.run_main(
            parents_case("acme widgets", 1), substrings={"acme": "Removed"}
        )

        self.assertEqual(code, 1)
        self.assertIn("MISMATCH", output)
        self.assertEqual((calls, written), ([], {}))


class TestOutputPrivacy(RunnerTestCase):
    def test_output_prints_no_merchant_or_expected_choice(self):
        wiring = self.wire("finance")

        _, output = self.run_main("finance", wiring)

        for case in wiring.cases:
            self.assertNotIn(case.merchant, output)
        self.assertIn("Pass 1:", output)
        for line in output.splitlines():
            if line.startswith("Pass "):
                self.assertRegex(line, r"Pass \d+: [0-9a-f]{12} ")


class TestProductionWiring(unittest.TestCase):
    def test_runner_sources_the_production_path(self):
        source = MODULE_PATH.read_text(encoding="utf-8")

        self.assertIn("production_categorizer_factory", source)
        self.assertIn("run_validation", source)
        self.assertIn("write_approval_record", source)
        self.assertIn("load_gold_cases", source)
        self.assertIn("enriched_transaction_llm_model", source)

    def test_enriched_branch_selects_its_own_model_not_the_database(self):
        # Legacy finance validation stays supported, so the model must follow
        # the protocol flag rather than the database name.
        source = MODULE_PATH.read_text(encoding="utf-8")

        self.assertIn("config.enriched_transaction_llm_model", source)
        self.assertIn("config.transaction_llm_model", source)


if __name__ == "__main__":
    unittest.main()


class TestApprovalWriteAtomicity(RunnerTestCase):
    """The approval record is replaced atomically, never truncated in place."""

    def approved_validation(self):
        return SimpleNamespace(approved=True, passes=[object()] * REQUIRED_PASSES)

    def write(self, database, validation):
        return real_write_approval_record(
            database=database,
            base_url="http://localhost:10100",
            model="exact-model-id",
            choices=FIXTURE["taxonomies"][database],
            cases=self.gold_cases(database),
            validation=validation,
            approval_path=self.approval_path,
        )

    def test_a_failed_replace_preserves_the_previous_record(self):
        wiring = self.wire("finance")
        self.assertEqual(self.run_main("finance", wiring)[0], 0)
        before = self.approval_path.read_bytes()

        with patch(
            "services.gold_validator.os.replace", side_effect=OSError("disk full")
        ):
            with self.assertRaises(OSError):
                self.write("parents_finance", self.approved_validation())

        self.assertEqual(self.approval_path.read_bytes(), before)
        self.assertEqual(
            sorted(p.name for p in self.tmp_path.iterdir() if p.name.endswith(".tmp")),
            [],
        )

    def test_a_successful_write_merges_into_the_existing_document(self):
        self.write("finance", self.approved_validation())
        self.write("parents_finance", self.approved_validation())

        document = json.loads(self.approval_path.read_text(encoding="utf-8"))
        self.assertEqual(sorted(document), ["finance", "parents_finance"])
        self.assertEqual(
            sorted(p.name for p in self.tmp_path.iterdir() if p.name.endswith(".tmp")),
            [],
        )

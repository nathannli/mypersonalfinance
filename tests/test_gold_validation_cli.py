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
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from services.gold_validator import (
    EnrichedPassResult,
    EnrichedValidationResult,
    write_approval_record as real_write_approval_record,
)
from services.transaction_categorization import CategorizationResult, ProviderAction
from services.transaction_llm_approval import (
    REQUIRED_PASSES,
    ApprovalError,
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
        opencodex_base_url="http://localhost:10100",
        opencodex_api_key="x",
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
        wiring = self.wire("parents_finance")

        code, _ = self.run_main("parents_finance", wiring)

        self.assertEqual(code, 0)
        document = json.loads(self.approval_path.read_text(encoding="utf-8"))
        self.assertEqual(sorted(document), ["parents_finance"])


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
        wiring = self.wire("parents_finance")

        code, _ = self.run_main("parents_finance", wiring)

        self.assertEqual(code, 0)
        # The factory is resolved once per run; one fresh categorizer per pass.
        self.assertEqual(wiring.factory_models, ["exact-model-id"])
        self.assertEqual(len(wiring.factory_calls), 3)

    def test_enriched_run_validates_with_the_enriched_model(self):
        cases = [object()]
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

    def test_enriched_rejects_a_non_finance_database(self):
        wiring = self.wire("parents_finance")

        with (
            patch.object(runner, "Config", fake_config),
            patch.object(
                runner, "DATABASE_CLASSES", {"parents_finance": wiring.fake_db_class}
            ),
        ):
            buffer = io.StringIO()
            with redirect_stdout(buffer):
                code = runner.main(["--database", "parents_finance", "--enriched"])

        self.assertEqual(code, 1)
        self.assertIn("finance", buffer.getvalue())
        self.assertEqual(wiring.written, {})


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

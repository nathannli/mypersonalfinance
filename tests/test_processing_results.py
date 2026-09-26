import unittest

from services.transaction_categorization import TransactionStatus
from utils.processing_results import (
    RUN_COMPLETE,
    RUN_FAILED,
    RUN_PARTIAL,
    ProcessingResults,
)


def totals(**counts) -> dict[TransactionStatus, int]:
    result = {status: 0 for status in TransactionStatus}
    for name, count in counts.items():
        result[TransactionStatus[name.upper()]] = count
    return result


class TestOutcomeReconciliation(unittest.TestCase):
    def test_totals_accumulate_across_files(self):
        results = ProcessingResults()

        results.add_success("a.csv", totals(inserted=2, duplicate=1), 3)
        results.add_success("b.csv", totals(inserted=1, shadow=2, ignored=1), 4)

        self.assertEqual(results.get_status_total(TransactionStatus.INSERTED), 3)
        self.assertEqual(results.get_status_total(TransactionStatus.DUPLICATE), 1)
        self.assertEqual(results.get_status_total(TransactionStatus.SHADOW), 2)
        self.assertEqual(results.get_status_total(TransactionStatus.IGNORED), 1)
        self.assertEqual(sum(results.totals.values()), 7)
        self.assertEqual(results.get_total_transactions(), 7)

    def test_inserted_count_excludes_non_insert_statuses(self):
        results = ProcessingResults()

        results.add_success(
            "a.csv",
            totals(
                inserted=1, duplicate=1, ignored=1, deleted=1, shadow=1, unresolved=1
            ),
            6,
        )

        self.assertEqual(results.get_total_inserted(), 1)

    def test_mismatched_totals_are_rejected(self):
        results = ProcessingResults()

        with self.assertRaises(ValueError):
            results.add_success("a.csv", totals(inserted=1), 3)

        self.assertEqual(results.results, [])
        self.assertEqual(sum(results.totals.values()), 0)

    def test_empty_file_reconciles_to_zero_rows(self):
        results = ProcessingResults()

        results.add_success("empty.csv", totals(), 0)

        self.assertEqual(results.get_run_status(), RUN_COMPLETE)
        self.assertEqual(sum(results.totals.values()), 0)

    def test_one_shadow_and_one_unresolved_row_reconcile_to_partial(self):
        # V54: an uncovered row stays typed unresolved while a covered row is
        # shadow, and every row still reconciles against the processed total.
        results = ProcessingResults()

        results.add_success("a.csv", totals(shadow=1, unresolved=1), 2)

        self.assertEqual(results.get_run_status(), RUN_PARTIAL)
        self.assertEqual(results.get_exit_code(), 0)
        self.assertEqual(results.get_status_total(TransactionStatus.SHADOW), 1)
        self.assertEqual(results.get_status_total(TransactionStatus.UNRESOLVED), 1)
        self.assertEqual(results.get_total_transactions(), 2)
        self.assertEqual(sum(results.totals.values()), 2)


class TestRunStatusAndExitCode(unittest.TestCase):
    def test_complete_when_only_settled_statuses(self):
        results = ProcessingResults()
        results.add_success(
            "a.csv", totals(inserted=2, duplicate=1, ignored=1, deleted=1), 5
        )

        self.assertEqual(results.get_run_status(), RUN_COMPLETE)
        self.assertEqual(results.get_exit_code(), 0)

    def test_shadow_or_unresolved_yields_partial_and_exit_zero(self):
        for label, counts in (
            ("shadow", totals(inserted=1, shadow=1)),
            ("unresolved", totals(inserted=1, unresolved=1)),
        ):
            with self.subTest(label=label):
                results = ProcessingResults()
                results.add_success("a.csv", counts, 2)

                self.assertEqual(results.get_run_status(), RUN_PARTIAL)
                self.assertEqual(results.get_exit_code(), 0)

    def test_file_failure_yields_failed_and_exit_one(self):
        results = ProcessingResults()
        results.add_success("a.csv", totals(inserted=1), 1)
        results.add_failure("b.csv", "boom")

        self.assertEqual(results.get_run_status(), RUN_FAILED)
        self.assertEqual(results.get_exit_code(), 1)

    def test_failure_outranks_partial(self):
        results = ProcessingResults()
        results.add_success("a.csv", totals(shadow=1), 1)
        results.add_failure("b.csv", "boom")

        self.assertEqual(results.get_run_status(), RUN_FAILED)


class TestUnresolvedReporting(unittest.TestCase):
    def test_unresolved_rows_are_recorded_with_reason(self):
        results = ProcessingResults()
        rows = [
            {"date": "2026-09-15", "merchant": "OPENAI", "reason": "timeout"},
            {"date": "2026-09-16", "merchant": "GROK XAI", "reason": "abstained"},
        ]

        results.add_success("a.csv", totals(unresolved=2), 2, rows)

        self.assertEqual(results.unresolved_rows, rows)
        self.assertEqual(
            len(results.unresolved_rows),
            results.get_status_total(TransactionStatus.UNRESOLVED),
        )

    def test_format_totals_lists_every_status(self):
        results = ProcessingResults()
        results.add_success("a.csv", totals(inserted=1), 1)

        rendered = results.format_totals()

        for status in TransactionStatus:
            self.assertIn(f"{status.value}=", rendered)


class TestSuggestedReporting(unittest.TestCase):
    def test_suggested_row_makes_the_run_partial(self):
        results = ProcessingResults()
        results.add_success("a.csv", totals(inserted=1, suggested=1), 2)

        # V40: a suggested row is unfinished work, so the run is never complete.
        self.assertEqual(results.get_run_status(), RUN_PARTIAL)
        self.assertEqual(results.get_exit_code(), 0)

    def test_suggested_alone_never_reports_complete(self):
        results = ProcessingResults()
        results.add_success("a.csv", totals(suggested=1), 1)

        self.assertEqual(results.get_run_status(), RUN_PARTIAL)

    def test_suggested_totals_still_reconcile(self):
        results = ProcessingResults()
        results.add_success("a.csv", totals(inserted=1, suggested=2), 3)

        self.assertEqual(sum(results.totals.values()), 3)
        self.assertEqual(results.get_status_total(TransactionStatus.SUGGESTED), 2)

    def test_suggestion_ids_are_collected_and_deduplicated(self):
        results = ProcessingResults()

        results.add_success(
            "a.csv", totals(suggested=2), 2, suggestion_ids=["abc", "xyz"]
        )
        results.add_success("b.csv", totals(suggested=1), 1, suggestion_ids=["abc"])

        self.assertEqual(results.suggestion_ids, ["abc", "xyz"])

    def test_no_suggestions_leaves_ids_empty(self):
        results = ProcessingResults()
        results.add_success("a.csv", totals(inserted=1), 1)

        self.assertEqual(results.suggestion_ids, [])


if __name__ == "__main__":
    unittest.main()

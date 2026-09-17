"""
Processing Results Tracker - Track and report transaction processing results.

This module provides a class to encapsulate result tracking logic for
processing credit card transaction files.

Every processed input row contributes exactly one typed outcome, so the
printed totals reconcile against the number of rows the loader produced.
"""

from collections.abc import Mapping, Sequence

from services.transaction_categorization import TransactionStatus

RUN_COMPLETE = "complete"
RUN_PARTIAL = "partial"
RUN_FAILED = "failed"


class ProcessingResults:
    """
    Track processing results for multiple files.

    Manages success/failure tracking and summary reporting for
    credit card transaction file processing.
    """

    def __init__(self):
        """Initialize empty results tracker."""
        self.results = []
        self.failed_files = []
        self.totals = {status: 0 for status in TransactionStatus}
        self.unresolved_rows = []
        self.suggestion_ids = []

    def add_success(
        self,
        file_name: str,
        totals: Mapping[TransactionStatus, int],
        total: int,
        unresolved_rows: Sequence[dict] = (),
        suggestion_ids: Sequence[str] = (),
    ) -> None:
        """
        Record a successful file processing.

        Args:
            file_name: Name of the processed file
            totals: Count of each typed outcome produced by the file
            total: Total number of transactions in the file
            unresolved_rows: Reason/merchant/date for each unresolved row
            suggestion_ids: Suggestion ids produced by the file

        Raises:
            ValueError: If the typed outcomes do not reconcile to `total`
        """
        counted = sum(totals.values())
        if counted != total:
            raise ValueError(
                f"Outcome totals for {file_name} do not reconcile: "
                f"{counted} outcomes for {total} processed rows"
            )
        for status, count in totals.items():
            self.totals[status] += count
        self.unresolved_rows.extend(unresolved_rows)
        # V35: identifiers only; full proposals stay in the private artifact.
        for suggestion_id in suggestion_ids:
            if suggestion_id not in self.suggestion_ids:
                self.suggestion_ids.append(suggestion_id)
        self.results.append(
            {
                "file": file_name,
                "status": "success",
                "inserted": totals.get(TransactionStatus.INSERTED, 0),
                "total": total,
                "totals": dict(totals),
            }
        )

    def add_failure(self, file_name: str, error: str) -> None:
        """
        Record a failed file processing.

        Args:
            file_name: Name of the failed file
            error: Error message describing the failure
        """
        self.failed_files.append({"file": file_name, "error": error})
        self.results.append(
            {
                "file": file_name,
                "status": "failed",
                "inserted": 0,
                "total": 0,
                "totals": {},
            }
        )

    def get_total_inserted(self) -> int:
        """Get total number of transactions inserted across all files."""
        return sum(r["inserted"] for r in self.results)

    def get_total_transactions(self) -> int:
        """Get total number of transactions processed across all files."""
        return sum(r["total"] for r in self.results)

    def get_successful_count(self) -> int:
        """Get number of successfully processed files."""
        return sum(1 for r in self.results if r["status"] == "success")

    def get_failed_count(self) -> int:
        """Get number of failed files."""
        return len(self.failed_files)

    def has_failures(self) -> bool:
        """Check if any files failed to process."""
        return len(self.failed_files) > 0

    def get_status_total(self, status: TransactionStatus) -> int:
        """Get the number of rows that produced the given typed outcome."""
        return self.totals[status]

    def get_run_status(self) -> str:
        """Return `complete`, `partial`, or `failed` for the whole run."""
        if self.has_failures():
            return RUN_FAILED
        # V40: a suggested row is unfinished work, exactly like a shadow row,
        # so a run that only produced suggestions is never `complete`.
        unfinished = (
            self.totals[TransactionStatus.SHADOW]
            + self.totals[TransactionStatus.UNRESOLVED]
            + self.totals[TransactionStatus.SUGGESTED]
        )
        return RUN_PARTIAL if unfinished > 0 else RUN_COMPLETE

    def get_exit_code(self) -> int:
        """Exit 1 only for a failed run; complete and partial both exit 0."""
        return 1 if self.get_run_status() == RUN_FAILED else 0

    def format_totals(self) -> str:
        """Render every typed outcome count in a stable order."""
        return ", ".join(
            f"{status.value}={self.totals[status]}" for status in TransactionStatus
        )

    def print_summary(self, total_files: int) -> None:
        """
        Print a formatted summary of processing results.

        Args:
            total_files: Total number of files attempted
        """
        print("\n" + "=" * 80)
        print("PROCESSING SUMMARY")
        print("=" * 80)

        if self.results:
            print("\nPer-file breakdown:")
            for result in self.results:
                if result["status"] == "success":
                    print(
                        f"  ✓ {result['file']}: {result['inserted']}/{result['total']} transactions inserted"
                    )
                else:
                    print(f"  ✗ {result['file']}: FAILED")

        if self.has_failures():
            print(f"\n{self.get_failed_count()} file(s) failed to process:")
            for failed in self.failed_files:
                print(f"  - {failed['file']}: {failed['error']}")

        if self.unresolved_rows:
            print(f"\n{len(self.unresolved_rows)} unresolved transaction(s):")
            for unresolved in self.unresolved_rows:
                print(
                    f"  - {unresolved['date']} {unresolved['merchant']}: "
                    f"{unresolved['reason']}"
                )

        if self.suggestion_ids:
            # V35: the local summary reports suggestion identifiers only. The
            # full proposal and its citations stay in the private artifact.
            print(f"\n{len(self.suggestion_ids)} category suggestion(s):")
            for suggestion_id in self.suggestion_ids:
                print(f"  - {suggestion_id}")

        print(
            f"\nTotal: {self.get_total_inserted()}/{self.get_total_transactions()} "
            f"transactions inserted from {self.get_successful_count()}/{total_files} file(s)"
        )
        print(f"Outcomes: {self.format_totals()}")
        print(f"Run status: {self.get_run_status()}")
        print("=" * 80)

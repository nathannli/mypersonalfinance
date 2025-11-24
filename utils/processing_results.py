"""
Processing Results Tracker - Track and report transaction processing results.

This module provides a class to encapsulate result tracking logic for
processing credit card transaction files.
"""


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

    def add_success(self, file_name: str, inserted: int, total: int) -> None:
        """
        Record a successful file processing.

        Args:
            file_name: Name of the processed file
            inserted: Number of transactions inserted
            total: Total number of transactions in the file
        """
        self.results.append(
            {
                "file": file_name,
                "status": "success",
                "inserted": inserted,
                "total": total,
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
            {"file": file_name, "status": "failed", "inserted": 0, "total": 0}
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

        print(
            f"\nTotal: {self.get_total_inserted()}/{self.get_total_transactions()} "
            f"transactions inserted from {self.get_successful_count()}/{total_files} file(s)"
        )
        print("=" * 80)

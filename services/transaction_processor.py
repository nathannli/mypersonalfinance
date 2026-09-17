"""
Transaction Processor - Process credit card transaction files.

This module provides a service class that orchestrates the processing
of credit card transaction files, including loading data and inserting
into the database.
"""

import os
from collections.abc import Sequence

import polars as pl

from db.finance_base import FinanceDB
from services.llm_categorizer import OpenCodexCategorizer
from services.transaction_categorization import TransactionStatus
from services.transaction_loader import TransactionLoader
from utils.processing_results import ProcessingResults


class TransactionProcessor:
    """
    Service for processing credit card transaction files.

    Orchestrates the workflow of loading transaction data from files
    and inserting them into the database.
    """

    def __init__(
        self,
        database: FinanceDB,
        loader: TransactionLoader,
        categorizer: OpenCodexCategorizer,
    ):
        """
        Initialize transaction processor.

        Args:
            database: Database instance for storing transactions
            loader: Transaction loader service for loading card data
        """
        self.database = database
        self.loader = loader
        self.categorizer = categorizer

    def _insert_transactions(
        self, df: pl.DataFrame, card_type: str
    ) -> tuple[dict[TransactionStatus, int], list[dict]]:
        """
        Insert transactions from DataFrame into database.

        Args:
            df: DataFrame containing transactions
            card_type: Type of credit card

        Returns:
            Tuple of (typed outcome totals, unresolved row details). Every
            processed row contributes exactly one typed outcome.
        """
        totals = {status: 0 for status in TransactionStatus}
        unresolved_rows: list[dict] = []

        for row in df.iter_rows(named=True):
            date = row["date"]
            merchant = row["merchant"]
            cost = row["cost"]
            cc_category = row["cc_category"]

            # Check if transaction already exists in expenses table
            if self.database.check_if_expense_exists(date, merchant, cost):
                totals[TransactionStatus.DUPLICATE] += 1
                continue

            print("\n\n")
            print("New transaction found")
            outcome = self.database.insert_expense(
                date,
                merchant,
                cost,
                card_type,
                cc_category,
                categorizer=self.categorizer,
            )
            totals[outcome.status] += 1
            if outcome.status == TransactionStatus.UNRESOLVED:
                unresolved_rows.append(
                    {
                        "date": date,
                        "merchant": merchant,
                        "reason": outcome.reason.value if outcome.reason else "unknown",
                    }
                )

        return totals, unresolved_rows

    def _process_single_file(
        self, card_type: str, file_path: str | None, file_name: str
    ) -> tuple[dict[TransactionStatus, int], int, list[dict]]:
        """
        Process a single transaction file.

        Args:
            card_type: Type of credit card
            file_path: Path to file (or None for online sources)
            file_name: Display name for the file

        Returns:
            Tuple of (typed outcome totals, total_rows, unresolved rows)

        Raises:
            Exception: If file processing fails
        """
        # Load data
        if file_path is None:
            print(f"Loading {card_type} data from online source")
        else:
            print(f"Loading {card_type} data from {file_path}")

        df = self.loader.load(card_type, file_path)
        print("Data loaded")

        # Insert transactions if DataFrame has data
        if df.height > 0:
            totals, unresolved_rows = self._insert_transactions(df, card_type)
            return (totals, df.height, unresolved_rows)
        else:
            print("No data to process in the file")
            return ({status: 0 for status in TransactionStatus}, 0, [])

    def process_files(
        self, card_type: str, files: Sequence[str | None]
    ) -> ProcessingResults:
        """
        Process multiple transaction files.

        Args:
            card_type: Type of credit card
            files: List of file paths to process (or [None] for online sources)

        Returns:
            ProcessingResults object with processing summary
        """
        results = ProcessingResults()

        # Process each file
        for idx, file_path in enumerate(files, 1):
            # Determine file name for display
            if file_path is None:
                file_name = f"{card_type} (online)"
            else:
                file_name = os.path.basename(file_path)

            # Print progress header
            if len(files) > 1:
                print(f"\n{'=' * 80}")
                print(f"Processing file {idx}/{len(files)}: {file_name}")
                print(f"{'=' * 80}\n")
            else:
                print(f"Processing: {file_name}\n")

            # Process the file
            try:
                totals, total, unresolved_rows = self._process_single_file(
                    card_type, file_path, file_name
                )
                results.add_success(file_name, totals, total, unresolved_rows)

            except KeyboardInterrupt:
                print("Keyboard interrupt")
                raise

            except Exception as e:
                print(f"ERROR processing file {file_name}: {e}")
                results.add_failure(file_name, str(e))
                # Continue processing remaining files
                continue

        return results

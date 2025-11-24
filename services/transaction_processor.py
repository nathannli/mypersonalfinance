"""
Transaction Processor - Process credit card transaction files.

This module provides a service class that orchestrates the processing
of credit card transaction files, including loading data and inserting
into the database.
"""

import os

import polars as pl

from classes.db.generics.finance_db import FinanceDB
from services.transaction_loader import TransactionLoader
from utils.processing_results import ProcessingResults


class TransactionProcessor:
    """
    Service for processing credit card transaction files.

    Orchestrates the workflow of loading transaction data from files
    and inserting them into the database.
    """

    def __init__(self, database: FinanceDB, loader: TransactionLoader):
        """
        Initialize transaction processor.

        Args:
            database: Database instance for storing transactions
            loader: Transaction loader service for loading card data
        """
        self.database = database
        self.loader = loader

    def _insert_transactions(self, df: pl.DataFrame, card_type: str) -> int:
        """
        Insert transactions from DataFrame into database.

        Args:
            df: DataFrame containing transactions
            card_type: Type of credit card

        Returns:
            Number of rows inserted
        """
        new_inserted_rows = 0

        for row in df.iter_rows(named=True):
            date = row["date"]
            merchant = row["merchant"]
            cost = row["cost"]
            cc_category = row["cc_category"]

            # Check if transaction already exists in expenses table
            if not self.database.check_if_expense_exists(date, merchant, cost):
                print("\n\n")
                print("New transaction found")
                self.database.insert_expense(
                    date, merchant, cost, card_type, cc_category
                )
                new_inserted_rows += 1

        return new_inserted_rows

    def _process_single_file(
        self, card_type: str, file_path: str, file_name: str
    ) -> tuple[int, int]:
        """
        Process a single transaction file.

        Args:
            card_type: Type of credit card
            file_path: Path to file (or None for online sources)
            file_name: Display name for the file

        Returns:
            Tuple of (inserted_rows, total_rows)

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
            inserted_rows = self._insert_transactions(df, card_type)
            return (inserted_rows, df.height)
        else:
            print("No data to process in the file")
            return (0, 0)

    def process_files(self, card_type: str, files: list[str]) -> ProcessingResults:
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
                inserted, total = self._process_single_file(
                    card_type, file_path, file_name
                )
                results.add_success(file_name, inserted, total)

            except KeyboardInterrupt:
                print("Keyboard interrupt")
                raise

            except Exception as e:
                print(f"ERROR processing file {file_name}: {e}")
                results.add_failure(file_name, str(e))
                # Continue processing remaining files
                continue

        return results

"""
Transaction Loader CLI - Command-line interface for loading credit card transactions.

This module provides a CLI handler class that manages argument parsing,
validation, and orchestration of the transaction loading process.
"""

import argparse
import os
from sys import exit

from classes.cc.card_registry import (
    get_card_type_names,
    get_file_based_card_types,
    get_online_card_types,
)
from classes.db.my_finance_db import MyFinanceDB
from classes.db.parents_finance_db import ParentsFinanceDB
from services.transaction_loader import TransactionLoader
from services.transaction_processor import TransactionProcessor


class TransactionLoaderCLI:
    """
    Command-line interface handler for transaction loading.

    Manages argument parsing, validation, and orchestration of the
    transaction loading workflow.
    """

    def __init__(self):
        """Initialize CLI with argument parser."""
        self.parser = self._build_parser()

    def _build_parser(self) -> argparse.ArgumentParser:
        """
        Build argument parser with all CLI options.

        Returns:
            Configured ArgumentParser instance
        """
        # Get supported card types from registry
        supported_card_types = get_card_type_names()
        card_types_str = ", ".join(supported_card_types)

        # Set up argument parser
        parser = argparse.ArgumentParser(
            description="Load credit card data into PostgreSQL database",
            epilog=f"""
Usage Examples:

  Single file:
    python load-cc-transactions.py --type cibc_mc --filepath /path/to/statement.csv --database finance

  Multiple files in folder:
    python load-cc-transactions.py --type cibc_mc --folder /path/to/statements/ --database finance

  Wealthsimple (online, no file needed):
    python load-cc-transactions.py --type ws_debit --database finance

Supported card types:
  {card_types_str}
        """,
            formatter_class=argparse.RawDescriptionHelpFormatter,
        )

        parser.add_argument(
            "--type",
            choices=supported_card_types,
            required=True,
            help="Type of credit card data to process",
        )
        parser.add_argument(
            "--filepath", required=False, help="Path to the transaction data csv file"
        )
        parser.add_argument(
            "--folder",
            required=False,
            help="Path to folder containing multiple transaction data csv files",
        )
        parser.add_argument(
            "--database",
            required=False,
            default="finance",
            help="Name of the database to use (finance or parents_finance)",
        )

        return parser

    def _validate_arguments(
        self, card_type: str, file_path: str, folder_path: str
    ) -> None:
        """
        Validate argument combinations for card type and file inputs.

        Args:
            card_type: The credit card type
            file_path: Path to single file (or None)
            folder_path: Path to folder (or None)

        Raises:
            ValueError: If argument combination is invalid
        """
        # Validate that --filepath and --folder are mutually exclusive
        if file_path and folder_path:
            raise ValueError(
                "Cannot provide both --filepath and --folder. Please provide only one."
            )

        online_types = get_online_card_types()
        file_based_types = get_file_based_card_types()

        if card_type in online_types:
            if file_path or folder_path:
                raise ValueError(
                    f"{card_type} doesn't use csv files, no need to provide --filepath or --folder"
                )
        elif card_type in file_based_types:
            if not file_path and not folder_path:
                raise ValueError(
                    f"Please provide either --filepath or --folder for {card_type} transactions"
                )

    def _build_file_list(
        self, card_type: str, file_path: str, folder_path: str
    ) -> list[str]:
        """
        Build list of files to process based on arguments.

        Args:
            card_type: The credit card type
            file_path: Path to single file (or None)
            folder_path: Path to folder (or None)

        Returns:
            List of file paths to process (or [None] for online card types)

        Raises:
            ValueError: If folder doesn't exist or is empty
        """
        online_types = get_online_card_types()

        if card_type in online_types:
            # Online cards don't use files, process once with no file
            return [None]
        elif folder_path:
            # Validate folder exists
            if not os.path.isdir(folder_path):
                raise ValueError(f"Folder does not exist: {folder_path}")

            # Get all files in the folder
            all_files = [
                os.path.join(folder_path, f)
                for f in os.listdir(folder_path)
                if os.path.isfile(os.path.join(folder_path, f))
            ]

            if not all_files:
                raise ValueError(f"Folder is empty: {folder_path}")

            print(f"Found {len(all_files)} files in folder: {folder_path}")
            return all_files
        elif file_path:
            return [file_path]
        else:
            return []

    def _get_database_instance(self, database_name: str):
        """
        Get database instance based on database name.

        Args:
            database_name: Name of the database ('finance' or 'parents_finance')

        Returns:
            Database instance

        Raises:
            ValueError: If database name is invalid
        """
        if database_name == "finance":
            return MyFinanceDB(debug=True)
        elif database_name == "parents_finance":
            return ParentsFinanceDB(debug=True)
        else:
            raise ValueError(
                f"Invalid database name: {database_name}. "
                f"Please choose from 'finance' or 'parents_finance'."
            )

    def run(self, args: argparse.Namespace) -> None:
        """
        Run the transaction loading process.

        Args:
            args: Parsed command-line arguments

        Exits:
            1 if an error occurs during processing
        """
        try:
            # Extract arguments
            card_type = args.type
            file_path = args.filepath
            folder_path = args.folder
            database_name = args.database

            # Validate argument combinations
            self._validate_arguments(card_type, file_path, folder_path)

            # Build list of files to process
            files_to_process = self._build_file_list(card_type, file_path, folder_path)

            # Load database
            print("Loading database...")
            database = self._get_database_instance(database_name)
            print("Database loaded\n")

            # Initialize services
            loader = TransactionLoader()
            processor = TransactionProcessor(database, loader)

            # Process files
            results = processor.process_files(card_type, files_to_process)

            # Print summary
            results.print_summary(len(files_to_process))

        except Exception as e:
            print(f"ERROR: {e}")
            self.parser.print_help()
            exit(1)

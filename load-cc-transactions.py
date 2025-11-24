import os
from sys import exit

import polars as pl

from classes.cc.card_registry import (
    get_card_class,
    get_card_type_names,
    get_file_based_card_types,
    get_online_card_types,
    requires_file,
)
from classes.db.generics.finance_db import FinanceDB
from classes.db.my_finance_db import MyFinanceDB
from classes.db.parents_finance_db import ParentsFinanceDB


def load_card_statement_df(card_type: str, file_path: str = None) -> pl.DataFrame:
    """
    Load credit card statement data based on card type.

    Args:
        card_type: Type of credit card
        file_path: Path to the transaction data file (not needed for online card types)

    Returns:
        pl.DataFrame: Loaded transaction data

    Raises:
        ValueError: If invalid card type or missing file path
    """
    # Get the appropriate statement class from the registry
    statement_class = get_card_class(card_type)

    # Instantiate and load data based on whether file is required
    if requires_file(card_type):
        return statement_class(file_path=file_path).get_df()
    else:
        return statement_class().get_df()


def validate_arguments(card_type: str, file_path: str, folder_path: str) -> None:
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


def build_file_list(card_type: str, file_path: str, folder_path: str) -> list[str]:
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


def get_database_instance(database_name: str) -> FinanceDB:
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
            f"Invalid database name: {database_name}. Please choose from 'finance' or 'parents_finance'."
        )


def print_processing_summary(
    results: list[dict], failed_files: list[dict], total_files: int
) -> None:
    """
    Print summary of processing results.

    Args:
        results: List of result dictionaries for each file
        failed_files: List of failed file dictionaries
        total_files: Total number of files processed
    """
    print("\n" + "=" * 80)
    print("PROCESSING SUMMARY")
    print("=" * 80)

    if results:
        print("\nPer-file breakdown:")
        for result in results:
            if result["status"] == "success":
                print(
                    f"  ✓ {result['file']}: {result['inserted']}/{result['total']} transactions inserted"
                )
            else:
                print(f"  ✗ {result['file']}: FAILED")

    if failed_files:
        print(f"\n{len(failed_files)} file(s) failed to process:")
        for failed in failed_files:
            print(f"  - {failed['file']}: {failed['error']}")

    total_inserted = sum(r["inserted"] for r in results)
    total_transactions = sum(r["total"] for r in results)
    successful_files = sum(1 for r in results if r["status"] == "success")

    print(
        f"\nTotal: {total_inserted}/{total_transactions} transactions inserted from {successful_files}/{total_files} file(s)"
    )
    print("=" * 80)


def insert_df_to_postgres(
    df: pl.DataFrame, finance_db: FinanceDB, card_type: str
) -> int:
    """
    Insert the processed credit card data into a PostgreSQL database using Polars.

    This function takes a Polars DataFrame containing credit card transaction data,
    retrieves category and subcategory information from the database, and prompts
    the user to categorize each transaction before inserting it into the expenses table.

    Args:
        df: Polars DataFrame containing the transaction data with columns for
            date, merchant, and cost

    Returns:
        int: Number of rows successfully inserted

    Note:
        The function checks if each transaction already exists in the database
        before prompting for categorization to avoid duplicates.
    """

    # For each row in df, check if (date, merchant, cost) exists in expenses table
    # If it does, skip it
    # If it doesn't, ask user to choose category and subcategory, then insert it
    new_inserted_rows = 0
    for row in df.iter_rows(named=True):
        date = row["date"]
        merchant = row["merchant"]
        cost = row["cost"]
        cc_category = row["cc_category"]
        # Check if transaction already exists in expenses table
        if not finance_db.check_if_expense_exists(date, merchant, cost):
            print("\n\n")
            print("New transaction found")
            finance_db.insert_expense(date, merchant, cost, card_type, cc_category)
            new_inserted_rows += 1

    return new_inserted_rows


if __name__ == "__main__":
    import argparse

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

    # Parse arguments
    args = parser.parse_args()

    try:
        # Extract arguments
        card_type = args.type
        file_path = args.filepath
        folder_path = args.folder
        database_name = args.database

        # Validate argument combinations
        validate_arguments(card_type, file_path, folder_path)

        # Build list of files to process
        files_to_process = build_file_list(card_type, file_path, folder_path)

        # Load database
        print("Loading database...")
        finance_db = get_database_instance(database_name)
        print("Database loaded\n")

    except Exception as e:
        print(f"ERROR: {e}")
        parser.print_help()
        exit(1)

    # Track results per file
    results = []
    failed_files = []

    # Process each file
    for idx, current_file_path in enumerate(files_to_process, 1):
        # Handle ws_debit/ws_credit which don't use files
        if current_file_path is None:
            file_name = f"{card_type} (online)"
        else:
            file_name = os.path.basename(current_file_path)

        if len(files_to_process) > 1:
            print(f"\n{'=' * 80}")
            print(f"Processing file {idx}/{len(files_to_process)}: {file_name}")
            print(f"{'=' * 80}\n")
        else:
            print(f"Processing: {file_name}\n")

        try:
            # Load data based on card type
            if current_file_path is None:
                print(f"Loading {card_type} data from online source")
            else:
                print(f"Loading {card_type} data from {current_file_path}")
            df = load_card_statement_df(card_type, current_file_path)
            print("Data loaded")

            # Check if the DataFrame has any rows before inserting
            if df.height > 0:
                try:
                    inserted_rows = insert_df_to_postgres(
                        df=df, finance_db=finance_db, card_type=card_type
                    )
                    results.append(
                        {
                            "file": file_name,
                            "status": "success",
                            "inserted": inserted_rows,
                            "total": df.height,
                        }
                    )
                except KeyboardInterrupt:
                    print("Keyboard interrupt")
                    exit()
            else:
                print("No data to process in the file")
                results.append(
                    {
                        "file": file_name,
                        "status": "success",
                        "inserted": 0,
                        "total": 0,
                    }
                )

        except Exception as e:
            print(f"ERROR processing file {file_name}: {e}")
            failed_files.append({"file": file_name, "error": str(e)})
            results.append(
                {"file": file_name, "status": "failed", "inserted": 0, "total": 0}
            )
            # Continue processing remaining files
            continue

    # Print summary
    print_processing_summary(results, failed_files, len(files_to_process))

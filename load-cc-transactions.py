import os
from sys import exit

import polars as pl

from classes.cc.amex import AmexStatement
from classes.cc.bmo import BMOStatement
from classes.cc.rbc_cc import RbcCcStatement
from classes.cc.rogers import RogersStatement
from classes.cc.simplii_debit import SimpliiDebitStatement
from classes.cc.simplii_visa import SimpliiVisaStatement
from classes.cc.wealthsimple_credit import WealthsimpleCreditStatement
from classes.cc.wealthsimple_debit import WealthsimpleDebitStatement
from classes.db.generics.finance_db import FinanceDB
from classes.db.my_finance_db import MyFinanceDB
from classes.db.parents_finance_db import ParentsFinanceDB


def extract_card_type_from_filename(file_path: str) -> str:
    """
    Extract the card type from the filename.

    Args:
        file_path: Path to the transaction data file

    Returns:
        str: The extracted card type

    Raises:
        ValueError: If the card type cannot be determined from the filename
    """
    # Get the base filename without directory path
    base_filename = os.path.basename(file_path)

    # List of valid card types
    valid_card_types = [
        "amex",
        "rogers",
        "simplii_visa",
        "simplii_debit",
        "bmo",
        "rbc_cc",
        "ws_debit",
        "ws_credit",
    ]

    # Check if any of the valid card types are in the filename
    for card_type in valid_card_types:
        if card_type in base_filename:
            return card_type

    # If no valid card type is found, raise an error
    raise ValueError(
        f"Could not determine card type from filename: {base_filename}. "
        f"Expected one of: {', '.join(valid_card_types)}"
    )


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

    print("\n\n")
    print(
        f"Successfully inserted {new_inserted_rows}/{df.height} rows into finance.expenses"
    )
    return new_inserted_rows


if __name__ == "__main__":
    import argparse

    # Set up argument parser
    parser = argparse.ArgumentParser(
        description="Load credit card data into PostgreSQL database"
    )
    parser.add_argument(
        "--type",
        choices=[
            "amex",
            "rogers",
            "simplii_visa",
            "simplii_debit",
            "bmo",
            "rbc_cc",
            "ws_debit",
            "ws_credit",
        ],
        required=False,
        default=None,
        help="Type of credit card data to process. If not provided, will be determined from the filename.",
    )
    parser.add_argument(
        "--filepath", required=False, help="Path to the transaction data csv file"
    )
    parser.add_argument(
        "--database",
        required=False,
        default="finance",
        help="Name of the database to use (finance or parents_finance)",
    )

    # Parse arguments and check if any are missing
    args = parser.parse_args()

    # If any required args are missing, parser.parse_args() will exit automatically
    # with the help message, but we'll handle any other issues
    try:
        card_type = args.type
        file_path = args.filepath

        # If card_type not provided, try to infer it from file_path
        if not card_type:
            if not file_path:
                raise ValueError("Either --type or --filepath must be provided")
            card_type = extract_card_type_from_filename(file_path)
            print(f"Successfully determined card type from filename: {card_type}")

        if card_type in {"ws_debit", "ws_credit"}:
            if file_path:
                print(
                    "Wealthsimple doesn't use csv files, no need to provide --filepath"
                )
                parser.print_help()
                exit(1)

        elif card_type in {
            "amex",
            "rogers",
            "simplii_visa",
            "simplii_debit",
            "bmo",
            "rbc_cc",
        }:
            if not file_path:
                print(f"Please provide --filepath for {card_type} transactions")
                parser.print_help()
                exit(1)

        else:
            print(f"Unknown card type: {card_type}")
            parser.print_help()
            exit(1)

        database_name = args.database

    except Exception as e:
        print(f"ERROR: {e}")
        parser.print_help()
        exit(1)

    # Load data based on card type
    if card_type == "amex":
        df = AmexStatement(file_path=file_path).get_df()
    elif card_type == "rogers":
        df = RogersStatement(file_path=file_path).get_df()
    elif card_type == "simplii_visa":
        df = SimpliiVisaStatement(file_path=file_path).get_df()
    elif card_type == "simplii_debit":
        df = SimpliiDebitStatement(file_path=file_path).get_df()
    elif card_type == "bmo":
        df = BMOStatement(file_path=file_path).get_df()
    elif card_type == "rbc_cc":
        df = RbcCcStatement(file_path=file_path).get_df()
    elif card_type == "ws_debit":
        df = WealthsimpleDebitStatement().get_df()
    elif card_type == "ws_credit":
        df = WealthsimpleCreditStatement().get_df()
    else:
        print(
            f"Invalid card type: {card_type}. Please choose from 'amex' or 'rogers' or 'simplii_visa' or 'simplii_debit' or 'bmo' or 'rbc_cc' or 'ws_debit' or 'ws_credit'."
        )
        exit()

    if database_name == "finance":
        finance_db = MyFinanceDB(debug=True)
    elif database_name == "parents_finance":
        finance_db = ParentsFinanceDB(debug=True)
    else:
        print(
            f"Invalid database name: {database_name}. Please choose from 'finance' or 'parents_finance'."
        )
        exit()

    # Check if the DataFrame has any rows before inserting
    if df.height > 0:
        try:
            insert_df_to_postgres(df=df, finance_db=finance_db, card_type=card_type)
        except KeyboardInterrupt:
            print("Keyboard interrupt")
            exit()
        print("\n\n")
    else:
        print(f"No data to process in the {card_type} file")

import polars as pl
from classes.db.generics.finance_db import FinanceDB
from classes.db.my_finance_db import MyFinanceDB
from classes.db.parents_finance_db import ParentsFinanceDB
from classes.cc.amex import AmexStatement
from classes.cc.rogers import RogersStatement

from sys import exit


def insert_df_to_postgres(df: pl.DataFrame, finance_db: FinanceDB, card_type: str) -> int:
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
        choices=["amex", "rogers"],
        required=True,
        help="Type of credit card data to process (amex or rogers)",
    )
    parser.add_argument(
        "--filepath", required=True, help="Path to the credit card data file"
    )
    parser.add_argument(
        "--database", required=True, help="Name of the database to use (finance or parents_finance)"
    )
    args = parser.parse_args()

    # Get file path and card type from arguments
    file_path = args.filepath
    card_type = args.type
    database_name = args.database


    # Load data based on card type
    if card_type == "amex":
        df = AmexStatement(file_path=file_path).get_df()
    elif card_type == "rogers":
        df = RogersStatement(file_path=file_path).get_df()
    else:
        print(f"Invalid card type: {card_type}. Please choose from 'amex' or 'rogers'.")
        exit()

    if database_name == "finance":
        finance_db = MyFinanceDB(debug=True)
    elif database_name == "parents_finance":
        finance_db = ParentsFinanceDB(debug=True)
    else:
        print(f"Invalid database name: {database_name}. Please choose from 'finance' or 'parents_finance'.")
        exit()

    # Check if the DataFrame has any rows before inserting
    if df.height > 0:
        try:
            insert_df_to_postgres(df=df, finance_db=finance_db, card_type=card_type)
        except KeyboardInterrupt:
            print("Keyboard interrupt")
            exit()
        print("\n\n")
        print(f"No data to process in the {card_type} file")

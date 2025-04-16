import argparse
import polars as pl
from classes.db.parents_finance_db import ParentsFinanceDB

DEBUG = True

def run(file_path: str):
    # Define schema for the Excel file
    schema = {
        "BANK": pl.Utf8,
        "YEAR": pl.Int64,
        "DATE": pl.Date,
        "DETAILSDescriptions": pl.Utf8,
        "DR_PAYMENTs": pl.Float64,
        "CR_RECEIPT": pl.Float64,
        "ACCT_subCODE": pl.Utf8,
        "ACCT_CODE": pl.Utf8,
    }

    # Read Excel file with defined schema
    df = pl.read_excel(file_path, schema_overrides=schema)

    # select only the columns we need
    df1 = df.select(
        pl.col("DATE"),
        pl.col("DETAILSDescriptions"),
        pl.col("DR_PAYMENTs"),
        pl.col("ACCT_CODE")
    )

    # rename the columns
    df2 = df1.rename({
        "DATE": "date",
        "DETAILSDescriptions": "merchant",
        "DR_PAYMENTs": "cost",
        "ACCT_CODE": "cc_category"
    })

    # drop rows where cost is negative
    df3 = df2.filter(pl.col("cost") > 0)

    # load parents db
    parents_db = ParentsFinanceDB(debug=DEBUG)

    # insert the expenses
    new_inserted_rows = 0
    for i, row in enumerate(df3.iter_rows(named=True)):
        print(f"Processing row {i+1}/{df3.height}")
        date = row["date"]
        merchant = row["merchant"]
        cost = row["cost"]
        cc_category = row["cc_category"]
        # Check if transaction already exists in expenses table
        if not parents_db.check_if_expense_exists(date, merchant, cost):
            print("\n\n")
            print("New transaction found")
            parents_db.insert_expense(date, merchant, cost, cc_category)
            new_inserted_rows += 1

    print("\n\n")
    print(f"Successfully inserted {new_inserted_rows}/{df3.height} rows into parents_finance.expenses")


if __name__ == "__main__":
    # Set up argument parser
    parser = argparse.ArgumentParser(
        description="Load credit card data into PostgreSQL database from pre-existing excel file"
    )
    parser.add_argument(
        "--filepath", required=True, help="Path to the credit card excel file"
    )
    args = parser.parse_args()
    file_path = args.filepath

    try:
        run(file_path)
    except KeyboardInterrupt:
        print("Keyboard interrupt")
        exit()

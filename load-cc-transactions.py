import polars as pl
import os
import sys
import psycopg
from configparser import ConfigParser
from dotenv import load_dotenv

load_dotenv()


# Connection string for Polars to connect to PostgreSQL database
POLARS_CONNECTION_STRING = os.getenv("POSTGRES_CONNECTION_STRING")


def load_amex_data(file_path: str) -> pl.DataFrame:
    """
    Load and process American Express transaction data from an Excel file.

    This function reads an Excel file containing Amex transaction data, identifies
    the header row, extracts relevant columns, and transforms the data into a
    standardized format for database insertion.

    Args:
        file_path: Path to the Excel file containing Amex transaction data

    Returns:
        pl.DataFrame: Processed DataFrame with standardized column names and data types

    Raises:
        SystemExit: If the file doesn't exist or required headers can't be found
    """
    # Check if file exists
    if not os.path.exists(file_path):
        print(f"Error: File {file_path} not found.")
        sys.exit(1)

    # Read the Excel file
    df = pl.read_excel(source=file_path, has_header=False)

    # Find the header row that contains 'Date', 'Description', 'Amount'
    header_row = None
    for i, row in enumerate(df.iter_rows()):
        if "Date" in row and "Description" in row and "Amount" in row:
            header_row = i
            break

    if header_row is None:
        print("Error: Could not find header row with 'Date', 'Description', 'Amount'")
        sys.exit(1)

    # Filter rows after the header row and drop unnecessary columns
    df1 = (
        df.with_row_index()
        .filter(pl.col("index") > header_row)
        .drop("index", "column_3")
    )

    # Rename columns to more descriptive names
    df2 = df1.rename(
        {
            "column_1": "date",
            "column_2": "merchant",
            "column_4": "cost",
        }
    )

    # Convert date strings to date objects
    df3 = df2.with_columns(pl.col("date").str.to_date(format="%d %b %Y"))

    # Convert amount strings to decimal numbers, removing dollar signs
    df4 = df3.with_columns(pl.col("cost").str.replace(r"\$", "").str.to_decimal())

    # Filter out rows where cost is negative (we only want expenses)
    df5 = df4.filter(pl.col("cost") > 0)

    return df5


def load_rogers_data(file_path: str) -> pl.DataFrame:
    """
    Load and process Rogers credit card transaction data from a CSV file.

    This function reads a CSV file containing Rogers transaction data and transforms
    it into a standardized format for database insertion.

    Args:
        file_path: Path to the CSV file containing Rogers transaction data

    Returns:
        pl.DataFrame: Processed DataFrame with standardized column names and data types

    Raises:
        Exception: If the file cannot be read or processed
    """
    # Read the CSV file with headers
    df = pl.read_csv(source=file_path, has_header=True)

    # Rename columns to standardized names
    df1 = df.rename({"Date": "date", "Merchant Name": "merchant", "Amount": "cost"})

    # Select only the columns we need
    df2 = df1.select(
        [
            "date",
            "merchant",
            "cost",
        ]
    )

    # Convert date strings to date objects
    df3 = df2.with_columns(pl.col("date").str.to_date(format="%Y-%m-%d"))

    # Convert cost strings to decimal numbers, removing dollar signs
    df4 = df3.with_columns(pl.col("cost").str.replace(r"\$", "").str.to_decimal())

    # Filter out rows where cost is negative (we only want expenses)
    df5 = df4.filter(pl.col("cost") > 0)

    return df5


def load_config(filename="database.ini", section="postgresql") -> dict:
    """
    Load database configuration from a configuration file.

    Args:
        filename: Path to the configuration file (default: 'database.ini')
        section: Section in the configuration file to read (default: 'postgresql')

    Returns:
        dict: Dictionary containing database connection parameters

    Raises:
        Exception: If the specified section is not found in the configuration file
    """
    parser = ConfigParser()
    parser.read(filename)

    # Get section, default to postgresql
    config = {}
    if parser.has_section(section):
        params = parser.items(section)
        for param in params:
            config[param[0]] = param[1]
    else:
        raise Exception(
            "Section {0} not found in the {1} file".format(section, filename)
        )
    return config


def postgres_insert(query: str, args: tuple) -> int:
    """
    Execute a SQL query with parameters against a PostgreSQL database.

    Args:
        query: SQL query string with parameter placeholders
        args: Tuple of parameter values to substitute into the query

    Returns:
        int: Number of rows affected by the query, or None if an error occurred

    Raises:
        None: Exceptions are caught and printed
    """
    config = load_config()
    try:
        with psycopg.connect(**config) as conn:
            with conn.cursor() as cur:
                cur.execute(query, args)
                conn.commit()
                return cur.rowcount
    except (psycopg.DatabaseError, Exception) as error:
        print(error)


def insert_to_postgres(df: pl.DataFrame) -> int:
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

    # Get reference tables for categories and subcategories
    categories = pl.read_database(
        query="select id as category_id, name as category from categories",
        connection=POLARS_CONNECTION_STRING,
    )
    subcategories = pl.read_database(
        query="select id as subcategory_id, name as subcategory from subcategories",
        connection=POLARS_CONNECTION_STRING,
    )

    # For each row in df, check if (date, merchant, cost) exists in expenses table
    # If it does, skip it
    # If it doesn't, ask user to choose category and subcategory, then insert it
    new_inserted_rows = 0
    for row in df.iter_rows():
        date = row["date"]
        merchant = row["merchant"]
        cost = row["cost"]

        # Check if transaction already exists in expenses table
        query = f"select id from expenses where date = '{date}' and merchant = '{merchant}' and cost = {cost}"
        result = pl.read_database(query=query, connection=POLARS_CONNECTION_STRING)

        if result.height > 0:
            # Skip if transaction already exists
            continue
        else:
            # Ask user to choose category and subcategory for new transaction
            print(
                f"Transaction on {date} for {merchant} with cost {cost} not found in expenses table"
            )

            # Display available categories
            print("Categories:")
            for category in categories.iter_rows():
                print(f"{category['category_id']}: {category['category']}")

            # Display available subcategories
            print("Subcategories:")
            for subcategory in subcategories.iter_rows():
                print(f"{subcategory['subcategory_id']}: {subcategory['subcategory']}")

            # Get user input for categorization
            category_id = input("Enter category id: ")
            subcategory_id = input("Enter subcategory id: ")

            # Insert the transaction with user-selected categories
            query = f"insert into expenses (date, merchant, cost, category_id, subcategory_id) values (%s, %s, %s, %s, %s)"
            postgres_insert(
                query=query, args=(date, merchant, cost, category_id, subcategory_id)
            )
            new_inserted_rows += 1

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

    # Parse arguments
    args = parser.parse_args()

    # Get file path and card type from arguments
    file_path = args.filepath
    card_type = args.type

    # Load data based on card type
    if card_type == "amex":
        df = load_amex_data(file_path=file_path)
    elif card_type == "rogers":
        df = load_rogers_data(file_path=file_path)

    # Check if the DataFrame has any rows before inserting
    if df.height > 0:
        insert_to_postgres(df=df)
    else:
        print(f"No data to process in the {card_type} file")

import argparse
import re
import polars as pl
import requests
import urllib.request
import tempfile
import os
from classes.db.parents_finance_db import ParentsFinanceDB

DISCORD_ALERT_BOT_URL = "http://195.168.1.95:30007/alert"
DEBUG = True

def run(file_path: str, cron: bool, original_file_path: str):

    # chequing file check
    chequing_file = False
    if "tdcheq" in file_path.lower():
        chequing_file = True

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
        pl.col("ACCT_subCODE"),
        pl.col("ACCT_CODE"),
    )

    # rename the columns
    df2 = df1.rename({
        "DATE": "date",
        "DETAILSDescriptions": "merchant",
        "DR_PAYMENTs": "cost",
        "ACCT_subCODE": "cc_sub_category",
        "ACCT_CODE": "cc_category"
    })

    # drop rows where cost is negative
    df3 = df2.filter(pl.col("cost") > 0)

    # load parents db
    parents_db = ParentsFinanceDB(debug=DEBUG, cron=cron)

    # insert the expenses
    new_inserted_rows = 0
    for i, row in enumerate(df3.iter_rows(named=True)):
        print(f"Processing row {i+1}/{df3.height}")
        date = row["date"]
        merchant = row["merchant"]
        cost = row["cost"]
        cc_category = row["cc_category"]
        cc_sub_category = row["cc_sub_category"]
        # special handling for cc_sub_category is Tfr=*
        if "Tfr=" in cc_sub_category:
            # check if transaction already exists in expenses table
            if parents_db.check_if_expense_exists(date, merchant, cost):
                # if yes, delete from expenses table
                expense_id = parents_db.get_expense_id(date, merchant, cost)
                parents_db.delete_expense(expense_id)
            continue
        # if tdcheq and cc_sub_category is "Tfr-*", then skip
        if chequing_file and "Tfr-" in cc_sub_category:
            continue
        # Check if transaction already exists in expenses table
        if not parents_db.check_if_expense_exists(date, merchant, cost):
            print("\n\n")
            print("New transaction found")
            parents_db.insert_expense(date, merchant, cost, cc_category)
            new_inserted_rows += 1

    print("\n\n")
    if cron:
        send_discord_message(f"Successfully inserted {new_inserted_rows}/{df3.height} rows into parents_finance.expenses for {original_file_path}")
        if parents_db.manual_intervention_required_expense_count > 0:
            message = f"Manual intervention required for {parents_db.manual_intervention_required_expense_count} expenses for {original_file_path}"
            send_discord_message(message)
    else:
        print(f"Successfully inserted {new_inserted_rows}/{df3.height} rows into parents_finance.expenses for {original_file_path}")


def obscure_credentials(message):
    # Regex to find URLs with credentials: scheme://username:password@host/...
    return re.sub(
        r'(ftp://)([^:/\s]+):([^@/\s]+)@',
        r'\1nnn:nnn@',
        message
    )


def send_discord_message(message):
    payload_dict = {"message": obscure_credentials(message)}
    requests.post(DISCORD_ALERT_BOT_URL, json=payload_dict)




def fetch_ftp_file(ftp_url: str) -> str:
    """
    Downloads a file from the given FTP URL to a temporary local file.

    Args:
        ftp_url (str): The FTP URL of the file to download.

    Returns:
        str: The path to the downloaded local temporary file.
    """
    tmp_file = tempfile.NamedTemporaryFile(delete=False)
    print(f"Downloading {ftp_url} to {tmp_file.name}...")
    urllib.request.urlretrieve(ftp_url, tmp_file.name)
    tmp_file.close()
    return tmp_file.name




if __name__ == "__main__":
    # Set up argument parser
    parser = argparse.ArgumentParser(
        description="Load credit card data into PostgreSQL database from pre-existing excel file"
    )
    parser.add_argument(
        "--filepath", required=True, help="Path to the credit card excel file"
    )
    parser.add_argument(
        "--cron", required=False, help="boolean, any input will trigger true"
    )
    args = parser.parse_args()
    file_path = args.filepath
    cron = True if args.cron else False

    if file_path.startswith("ftp://"):
        local_file_path = fetch_ftp_file(file_path)
    else:
        local_file_path = file_path

    try:
        run(local_file_path, cron, file_path)
    except KeyboardInterrupt:
        print("Keyboard interrupt")
        exit()
    except Exception as e:
        if cron:
            send_discord_message(f"Error for {file_path}: {e}")
        else:
            print(f"Error for {file_path}: {e}")
        exit()
    finally:
        if local_file_path != file_path:
            os.remove(local_file_path)

import polars as pl
from wealthsimpleton import wealthsimpleton as ws

from classes.cc.generics.credit_card_statement import CreditCardStatement


class WealthsimpleCreditStatement(CreditCardStatement):
    def __init__(self, file_path: str):
        super().__init__(type="ws_credit", file_path=file_path)
        self.load_data()

    def load_data(self) -> None:
        """
        Load and process Wealthsimple credit card transaction data using the wealthsimpleton API.

        This function retrieves credit card transaction data from Wealthsimple using the
        wealthsimpleton library, filters for purchase transactions, and transforms the data
        into a standardized format for database insertion.

        The function performs the following transformations:
        1. Fetches transactions using wealthsimpleton.get_transactions()
        2. Filters to only include "Purchase" type transactions
        3. Renames columns to standardized names (description -> merchant, amount -> cost)
        4. Adds a placeholder cc_category column
        5. Converts date strings from ISO format to date objects
        6. Parses cost strings (removes currency symbols) and converts to float

        Returns:
            None: Sets self.df with the processed DataFrame

        Raises:
            Any exceptions from the wealthsimpleton library or data processing
        """

        transactions: list[dict] = ws.get_transactions()
        df = pl.DataFrame(transactions)
        df1 = df.filter(pl.col("type") == "Purchase")

        # Rename columns to more normalized names
        df2 = df1.rename(
            {
                "description": "merchant",
                "amount": "cost",
            }
        )
        # Add a dummy cc_category column with None values
        df3 = df2.with_columns(pl.lit(None).alias("cc_category"))

        # Convert date strings to date objects
        df4 = df3.with_columns(pl.col("date").str.to_date(format="%Y-%m-%dT%H:%M:%S"))

        # parse float values from after the $ sign
        df5 = df4.with_columns(
            pl.col("cost")
            .str.replace_all(
                r"[^\d.\-]", ""
            )  # remove everything except digits, dot, minus
            .cast(pl.Float64)
        )

        self.df = df5

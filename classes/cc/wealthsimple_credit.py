import polars as pl
from wealthsimpleton import wealthsimpleton as ws

from classes.cc.generics.online_card_statement import OnlineCardStatement


class WealthsimpleCreditStatement(OnlineCardStatement):
    def __init__(self):
        super().__init__(type="ws_credit")

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
        print("================================================")
        print("load data start from wealthsimple_credit.py")
        transactions: list[dict] = ws.get_transactions(
            account_activity_url_suffix=self.config.ws_credit_link
        )
        df = pl.DataFrame(transactions)
        df1 = df.filter(pl.col("type") == "Purchase")

        # merge description & type
        df2 = df1.with_columns(
            pl.concat_str(
                [pl.col("type"), pl.col("description")], separator=": "
            ).alias("merchant")
        )

        # Rename columns to more normalized names
        df3 = df2.rename(
            {
                "amount": "cost",
            }
        )

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

        df6 = df5.select(
            pl.col("date"),
            pl.col("merchant"),
            pl.col("cost"),
            pl.lit(None).alias("cc_category"),
        )
        print("================================================")
        print("load data end from wealthsimple_credit.py")
        print(f"{df6=}")
        self.df = df6

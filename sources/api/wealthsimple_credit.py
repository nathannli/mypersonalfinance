import polars as pl

from sources.base import FileBasedCardStatement


class WealthsimpleCreditStatement(FileBasedCardStatement):
    purchase = "Purchase"
    refund = "Refund"
    acceptable_types = [purchase, refund]

    def __init__(self, file_path: str):
        super().__init__(type="ws_credit", file_path=file_path)

    def load_data(self) -> None:
        """
        Load Wealthsimple credit card activity scraped by
        scripts/download_wealthsimple_transactions.py.

        Filters for purchase and refund transactions and transforms the data
        into a standardized format for database insertion.

        The function performs the following transformations:
        1. Reads the scraped CSV (description, type, amount, date)
        2. Filters to only include "Purchase" type transactions
        3. Renames columns to standardized names (description -> merchant, amount -> cost)
        4. Adds a placeholder cc_category column
        5. Converts YYYY-MM-DD date strings to date objects
        6. Parses cost strings (removes currency symbols) and converts to float

        Returns:
            None: Sets self.df with the processed DataFrame

        Raises:
            Any exceptions from reading or processing the CSV
        """
        print("================================================")
        print("load data start from wealthsimple_credit.py")
        df = pl.read_csv(
            self.file_path,
            schema={
                "description": pl.Utf8,
                "type": pl.Utf8,
                "amount": pl.Utf8,
                "date": pl.Utf8,
            },
        )
        df1 = df.filter(pl.col("type").is_in(self.acceptable_types))

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
        df4 = df3.with_columns(pl.col("date").str.to_date(format="%Y-%m-%d"))

        # parse float values from after the $ sign
        df5 = df4.with_columns(
            pl.col("cost")
            .str.replace_all(
                r"[^\d.\-]", ""
            )  # remove everything except digits, dot, minus
            .cast(pl.Float64)
        )

        # refund transaction types should have negative costs
        df6 = df5.with_columns(
            pl.when(pl.col("type") == self.refund)
            .then(-pl.col("cost"))
            .otherwise(pl.col("cost"))
            .alias("cost")
        )

        df7 = df6.select(
            pl.col("date"),
            pl.col("merchant"),
            pl.col("cost"),
            pl.lit(None).alias("cc_category"),
        )
        print("================================================")
        print("load data end from wealthsimple_credit.py")
        print(f"{df7=}")
        self.df = df7

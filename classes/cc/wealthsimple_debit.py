import polars as pl

from classes.cc.generics.credit_card_statement import CreditCardStatement


class WealthsimpleDebitStatement(CreditCardStatement):
    def __init__(self, file_path: str):
        super().__init__(type="ws_debit", file_path=file_path)

    def load_data(self) -> None:
        """
        Load and process Wealthsimple transaction data from a CSV file.

        This function reads a CSV file containing Wealthsimple transaction data, identifies
        the header row, extracts relevant columns, and transforms the data into a
        standardized format for database insertion.

        Args:
            file_path: Path to the CSV file containing Wealthsimple transaction data

        Returns:
            pl.DataFrame: Processed DataFrame with standardized column names and data types

        Raises:
            ValueError: If the file doesn't exist or required headers can't be found
        """
        df = pl.read_csv(source=self.file_path, has_header=True)

        # Rename columns to more normalized names
        df2 = df.rename(
            {
                "description": "merchant",
                "amount": "cost",
            }
        )
        # Add a dummy cc_category column with None values
        df3 = df2.with_columns(pl.lit(None).alias("cc_category"))

        # Convert date strings to date objects
        df4 = df3.with_columns(pl.col("date").str.to_date(format="%Y-%m-%d"))

        # Filter out rows where cost is null (we only want expenses)
        df6 = df4.filter(pl.col("cost").is_not_null())

        # Only keep costs that are negative
        df7 = df6.filter(pl.col("cost") < 0)

        # remove rows where merchant contains ("rogers", "amex")
        df8 = df7.filter(
            ~pl.col("merchant").str.to_lowercase().str.contains("rogers|amex|bmo")
        )

        # remove rows where merchant equals to "Transfer out"
        df9 = df8.filter(pl.col("merchant") != "Transfer out")

        # turn cost into positive
        df10 = df9.with_columns(pl.col("cost").abs().alias("cost"))

        self.df = df10

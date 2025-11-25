import polars as pl

from classes.cc.generics.file_based_card_statement import FileBasedCardStatement


class CibcMcStatement(FileBasedCardStatement):
    def __init__(self, file_path: str):
        super().__init__(type="cibc_mc", file_path=file_path)

    def load_data(self) -> None:
        """
        Load and process CIBC Mastercard transaction data from a CSV file.

        This function reads a CSV file containing CIBC MC transaction data and transforms
        it into a standardized format for database insertion.

        The CSV format is:
        - Column 0: Transaction Date (YYYY-MM-DD)
        - Column 1: Merchant Description
        - Column 2: Debit Amount (purchases/charges)
        - Column 3: Credit Amount (payments/refunds)
        - Column 4: Card Number (masked)

        Args:
            file_path: Path to the CSV file containing CIBC MC transaction data

        Returns:
            pl.DataFrame: Processed DataFrame with standardized column names and data types

        Raises:
            Exception: If the file cannot be read or processed
        """
        # Read the CSV file without headers (CIBC CSVs have no header row)
        df = pl.read_csv(source=self.file_path, has_header=False)

        # Rename columns to normalized names
        df1 = df.rename(
            {
                "column_1": "date",
                "column_2": "merchant",
                "column_3": "cost",
                "column_4": "credit",
                "column_5": "card_number",
            }
        )

        # Convert date strings to date objects (already in YYYY-MM-DD format)
        df2 = df1.with_columns(pl.col("date").str.to_date(format="%Y-%m-%d"))

        # Filter out payments (they appear in the credit column with "PAYMENT" in merchant)
        df3 = df2.filter(~(pl.col("merchant").str.contains("PAYMENT")))

        # Filter out rows where cost (debit) is null (we only want expenses, not credits/refunds)
        df4 = df3.filter(pl.col("cost").is_not_null())

        # Add cc_category as None
        df5 = df4.with_columns(pl.lit(None).alias("cc_category"))

        # Select only the columns we need
        df6 = df5.select(["date", "merchant", "cost", "cc_category"])

        self.df = df6

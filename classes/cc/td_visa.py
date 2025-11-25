import polars as pl

from classes.cc.generics.file_based_card_statement import FileBasedCardStatement


class TdVisaStatement(FileBasedCardStatement):
    def __init__(self, file_path: str):
        super().__init__(type="td_visa", file_path=file_path)

    def load_data(self) -> None:
        """
        Load and process TD Visa transaction data from a CSV file.

        This function reads a CSV file containing TD Visa transaction data and transforms
        it into a standardized format for database insertion.

        The CSV format is:
        - Column 0: Transaction Date (MM/DD/YYYY)
        - Column 1: Merchant Description
        - Column 2: Debit Amount (expenses)
        - Column 3: Credit Amount (payments/refunds)
        - Column 4: Balance

        Args:
            file_path: Path to the CSV file containing TD Visa transaction data

        Returns:
            pl.DataFrame: Processed DataFrame with standardized column names and data types

        Raises:
            Exception: If the file cannot be read or processed
        """
        # Read the CSV file without headers (TD Visa CSVs have no header row)
        df = pl.read_csv(source=self.file_path, has_header=False)

        # Rename columns to normalized names
        df1 = df.rename(
            {
                "column_1": "date",
                "column_2": "merchant",
                "column_3": "cost",
                "column_4": "credit",
                "column_5": "balance",
            }
        )

        # Convert date strings to date objects
        df2 = df1.with_columns(pl.col("date").str.to_date(format="%m/%d/%Y"))

        # Filter out payments (they appear in the credit column with "PAYMENT" in merchant)
        df3 = df2.filter(
            ~(pl.col("merchant").str.contains("PAYMENT"))
            & ~(pl.col("merchant").str.contains("REWARDS REDEMPTION"))
        )

        # turn the credit column to negative
        df4 = df3.with_columns(pl.col("credit").cast(pl.Float64).neg())

        # coalesce cost and credit to a single column
        df5 = df4.with_columns(
            pl.coalesce(
                pl.col("cost").cast(pl.Float64), pl.col("credit").cast(pl.Float64)
            ).alias("new_cost")
        )

        # Add cc_category as None
        df6 = df5.with_columns(pl.lit(None).alias("cc_category"))

        # Select only the columns we need
        df7 = df6.select(["date", "merchant", "new_cost", "cc_category"])

        # rename
        df8 = df7.rename({"new_cost": "cost"})

        self.df = df8

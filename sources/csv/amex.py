import polars as pl
from pathlib import Path

from sources.base import FileBasedCardStatement


class AmexStatement(FileBasedCardStatement):
    def __init__(self, file_path: str):
        super().__init__(type="amex", file_path=file_path)

    def load_data(self) -> None:
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
            ValueError: If the file doesn't exist or required headers can't be found
        """
        if Path(self.file_path).suffix.lower() == ".csv":
            df = pl.read_csv(
                source=self.file_path,
                schema_overrides={
                    "Date": pl.Utf8,
                    "Description": pl.Utf8,
                    "Amount": pl.Utf8,
                },
            ).select(
                pl.col("Date").alias("date"),
                pl.col("Description").alias("merchant"),
                pl.col("Amount").alias("cost"),
            )
            self.df = self._standardize(df)
            return

        df = pl.read_excel(source=self.file_path, has_header=False)

        # Find the header row that contains 'Date', 'Description', 'Amount'
        header_row = None
        for i, row in enumerate(df.iter_rows()):
            if "Date" in row and "Description" in row and "Amount" in row:
                header_row = i
                break

        if header_row is None:
            raise ValueError(
                "Could not find header row with 'Date', 'Description', 'Amount'"
            )

        headers = list(df.row(header_row))
        columns = {
            name: f"column_{headers.index(name) + 1}"
            for name in ("Date", "Description", "Amount")
        }

        # Filter rows after the header row and select transaction columns
        df1 = (
            df.with_row_index()
            .filter(pl.col("index") > header_row)
            .select(
                pl.col(columns["Date"]).alias("date"),
                pl.col(columns["Description"]).alias("merchant"),
                pl.col(columns["Amount"]).alias("cost"),
            )
        )

        self.df = self._standardize(df1)

    @staticmethod
    def _standardize(df: pl.DataFrame) -> pl.DataFrame:
        # Add a dummy cc_category column with None values
        df = df.with_columns(pl.lit(None).alias("cc_category"))

        # Convert date strings to date objects with handling for both formats
        df = df.with_columns(
            pl.col("date")
            .str.replace(". ", " ", literal=True)
            .str.to_date(format="%d %b %Y")
        )

        # Convert amount strings to decimal numbers, removing dollar signs and commas
        df = df.with_columns(
            pl.col("cost").str.replace_all(r"[$,]", "").str.to_decimal(scale=2)
        )

        # Filter out rows where merchant is "PAYMENT RECEIVED - THANK YOU"
        # This is a bill payment to Amex, not an expense
        return df.filter(
            ~pl.col("merchant").str.contains("PAYMENT RECEIVED - THANK YOU")
        ).select("date", "merchant", "cost", "cc_category")


class AmexAnnualStatement(FileBasedCardStatement):
    def __init__(self, file_path: str):
        super().__init__(type="amex_annual", file_path=file_path)

    def load_data(self) -> None:
        """
        Load and process American Express annual statement data from an csv file.
        """
        schema = {
            "Category": pl.Utf8,
            "Card Member": pl.Utf8,
            "Account Number": pl.Utf8,
            "Sub-Category": pl.Utf8,
            "Date": pl.Utf8,
            "Month-Billed": pl.Utf8,
            "Transaction": pl.Utf8,
            "Charges $": pl.Utf8,
            "Credits $": pl.Utf8,
        }
        df = pl.read_csv(source=self.file_path, has_header=True, schema=schema)
        df = df.with_columns(pl.col("Date").str.to_date(format="%d/%m/%Y"))
        df = df.with_columns(
            pl.col("Charges $").str.replace(",", "").str.to_decimal(scale=2)
        )
        df = df.with_columns(
            pl.col("Credits $").str.replace(",", "").str.to_decimal(scale=2)
        )

        # merge Charges $ and Credits $, but credits should be negative
        df = df.with_columns(pl.col("Credits $").mul(-1))
        df = df.with_columns(
            pl.coalesce(pl.col("Charges $"), pl.col("Credits $")).alias("cost")
        )
        df = df.select("Date", "Transaction", "cost")
        df = df.rename(
            {
                "Date": "date",
                "Transaction": "merchant",
            }
        )
        df = df.with_columns(pl.lit(None).alias("cc_category"))

        self.df = df

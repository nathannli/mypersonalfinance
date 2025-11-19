import polars as pl

from classes.cc.generics.file_based_card_statement import FileBasedCardStatement


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

        # Filter rows after the header row and drop unnecessary columns
        df1 = (
            df.with_row_index()
            .filter(pl.col("index") > header_row)
            .drop("index", "column_3")
        )

        # Rename columns to more normalized names
        df2 = df1.rename(
            {
                "column_1": "date",
                "column_2": "merchant",
                "column_5": "cost",
            }
        )
        # Add a dummy cc_category column with None values
        df3 = df2.with_columns(pl.lit(None).alias("cc_category"))

        # Convert date strings to date objects with handling for both formats
        # For months with 3 letters or less (e.g., "May"), use "%d %b %Y"
        # For months with more than 3 letters (e.g., "Apr."), use "%d %b. %Y"
        # Filter dates with period (e.g., "Apr.") and convert them
        df_with_period = df3.filter(pl.col("date").str.contains(r"\. "))
        df_with_period = df_with_period.with_columns(
            pl.col("date").str.to_date(format="%d %b. %Y")
        )

        # Filter dates without period (e.g., "May") and convert them
        df_without_period = df3.filter(~pl.col("date").str.contains(r"\. "))
        df_without_period = df_without_period.with_columns(
            pl.col("date").str.to_date(format="%d %b %Y")
        )

        # Union the two dataframes
        df4 = pl.concat([df_with_period, df_without_period])

        # Convert amount strings to decimal numbers, removing dollar signs and commas
        df5 = df4.with_columns(
            pl.col("cost").str.replace(r"\$", "").str.replace(",", "").str.to_decimal()
        )

        # Filter out rows where merchant is "PAYMENT RECEIVED - THANK YOU"
        # This is a bill payment to Amex, not an expense
        df6 = df5.filter(
            ~pl.col("merchant").str.contains("PAYMENT RECEIVED - THANK YOU")
        )

        self.df = df6


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
        df = df.with_columns(pl.col("Charges $").str.replace(",", "").str.to_decimal())
        df = df.with_columns(pl.col("Credits $").str.replace(",", "").str.to_decimal())

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

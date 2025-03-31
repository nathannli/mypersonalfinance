from classes.cc.generics.credit_card_statement import CreditCardStatement
import polars as pl

class RogersStatement(CreditCardStatement):
    def __init__(self, file_path: str):
        super().__init__(type="rogers", file_path=file_path)

    def load_data(self) -> pl.DataFrame:
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
        df = pl.read_csv(source=self.file_path, has_header=True)

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

        self.df = df5
        return self.df

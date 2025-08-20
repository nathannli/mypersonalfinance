import polars as pl

from classes.cc.generics.file_based_card_statement import FileBasedCardStatement


class RbcCcStatement(FileBasedCardStatement):
    def __init__(self, file_path: str):
        super().__init__(type="rbc_cc", file_path=file_path)

    def load_data(self) -> None:
        """
        Load and process RBC credit card transaction data from a CSV file.

        This function reads a CSV file containing RBC transaction data and transforms
        it into a standardized format for database insertion.

        Args:
            file_path: Path to the CSV file containing RBC transaction data

        Returns:
            pl.DataFrame: Processed DataFrame with standardized column names and data types

        Raises:
            Exception: If the file cannot be read or processed
        """
        schema = {
            "Account Type": pl.Utf8,
            "Account Number": pl.Utf8,
            "Transaction Date": pl.Date,
            "Cheque Number": pl.Utf8,
            "Description 1": pl.Utf8,
            "Description 2": pl.Utf8,
            "CAD$": pl.Decimal(10, 2),
            "USD$": pl.Decimal(10, 2),
        }
        # Read the CSV file with headers
        df = pl.read_csv(
            source=self.file_path,
            has_header=True,
            schema=schema,
            truncate_ragged_lines=True,
        )

        # Rename columns to normalized names
        df1 = df.rename(
            {"Transaction Date": "date", "Description 1": "merchant", "CAD$": "cost"}
        )

        # Select only the columns we need
        df2 = df1.select(["date", "merchant", "cost"])

        # add cc_category as None
        df3 = df2.with_columns(pl.lit(None).alias("cc_category"))

        # mult by -1
        df4 = df3.with_columns(pl.col("cost").mul(-1).cast(pl.Decimal(10, 2)))

        self.df = df4

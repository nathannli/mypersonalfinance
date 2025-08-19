import polars as pl

from classes.cc.generics.file_based_card_statement import FileBasedCardStatement
from classes.cc.ref_data import simplii_visa_cc_merchant_name_to_category_ref


class SimpliiVisaStatement(FileBasedCardStatement):
    def __init__(self, file_path: str):
        super().__init__(type="simplii_visa", file_path=file_path)

    def load_data(self) -> None:
        """
        Load and process Simplii Visa transaction data from a CSV file.

        This function reads a CSV file containing Simplii Visa transaction data, identifies
        the header row, extracts relevant columns, and transforms the data into a
        standardized format for database insertion.

        Args:
            file_path: Path to the CSV file containing Simplii Visa transaction data

        Returns:
            pl.DataFrame: Processed DataFrame with standardized column names and data types

        Raises:
            ValueError: If the file doesn't exist or required headers can't be found
        """
        df = pl.read_csv(source=self.file_path, has_header=True)

        # Rename columns to more normalized names
        df2 = df.rename(
            {
                "Date": "date",
                " Transaction Details": "merchant",
                " Funds Out": "cost",
            }
        )
        # Add a dummy cc_category column with None values
        df3 = df2.with_columns(pl.lit(None).alias("cc_category"))

        # Convert date strings to date objects
        df4 = df3.with_columns(pl.col("date").str.to_date(format="%m/%d/%Y"))

        # Filter out rows where cost is null (we only want expenses)
        df6 = df4.filter(pl.col("cost").is_not_null())

        self.df = df6

    @staticmethod
    def auto_match_category() -> tuple[str, str]:
        """
        This is static because this card is only used for restaurants.
        """
        return simplii_visa_cc_merchant_name_to_category_ref["Restaurants"]

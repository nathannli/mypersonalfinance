from classes.cc.generics.credit_card_statement import CreditCardStatement
import polars as pl


class BMOStatement(CreditCardStatement):
    def __init__(self, file_path: str):
        super().__init__(type="bmo", file_path=file_path)

    def load_data(self) -> None:
        """
        Load and process BMO transaction data from an CSV file.

        This function reads an CSV file containing BMO transaction data, identifies
        the header row, extracts relevant columns, and transforms the data into a
        standardized format for database insertion.

        Args:
            file_path: Path to the CSV file containing BMO transaction data

        Returns:
            pl.DataFrame: Processed DataFrame with standardized column names and data types

        Raises:
            ValueError: If the file doesn't exist or required headers can't be found
        """
        df = pl.read_csv(source=self.file_path, has_header=False)

        # Find the header row that contains 'Date', 'Description', 'Amount'
        header_row = None
        for i, row in enumerate(df.iter_rows()):
            if (
                "Posting Date" in row
                and "Description" in row
                and "Transaction Amount" in row
            ):
                header_row = i
                break

        if header_row is None:
            raise ValueError(
                "Could not find header row with 'Posting Date', 'Description', 'Transaction Amount'"
            )

        # Filter rows after the header row and drop unnecessary columns
        df1 = (
            df.with_row_index()
            .filter(pl.col("index") > header_row)
            .drop("index", "column_1", "column_2", "column_4")
        )

        # Rename columns to more normalized names
        df2 = df1.rename(
            {
                "column_3": "date",
                "column_6": "merchant",
                "column_5": "cost",
            }
        )
        # Add a dummy cc_category column with None values
        df3 = df2.with_columns(pl.lit(None).alias("cc_category"))

        # Convert date strings to date objects
        df4 = df3.with_columns(pl.col("date").str.to_date(format="%Y%m%d"))

        # Convert amount strings to decimal numbers, removing dollar signs and commas
        df5 = df4.with_columns(pl.col("cost").str.to_decimal())

        # Filter out rows where cost is negative (we only want expenses)
        df6 = df5.filter(pl.col("cost") > 0)

        self.df = df6

from classes.cc.generics.credit_card_statement import CreditCardStatement
import polars as pl

class AmexStatement(CreditCardStatement):
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
            raise ValueError("Could not find header row with 'Date', 'Description', 'Amount'")

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
                "column_4": "cost",
            }
        )

        # Convert date strings to date objects
        df3 = df2.with_columns(pl.col("date").str.to_date(format="%d %b. %Y"))

        # Convert amount strings to decimal numbers, removing dollar signs
        df4 = df3.with_columns(pl.col("cost").str.replace(r"\$", "").str.to_decimal())

        # Filter out rows where cost is negative (we only want expenses)
        df5 = df4.filter(pl.col("cost") > 0)

        self.df = df5

import polars as pl

from classes.cc.generics.credit_card_statement import CreditCardStatement


class CanadianTireStatement(CreditCardStatement):
    def __init__(self, file_path: str):
        super().__init__(type="canadian_tire", file_path=file_path)

    def load_data(self) -> None:
        """
        Load and process Canadian Tire credit card transaction data from a CSV file.

        This function reads a CSV file containing Canadian Tire transaction data and transforms
        it into a standardized format for database insertion.
        """
        df = pl.read_csv(source=self.file_path, has_header=False)

        # Find the header row that contains 'REF', 'TRANSACTION DATE', 'POSTED DATE', 'TYPE', 'DESCRIPTION', 'Category', 'AMOUNT'
        header_row = None
        for i, row in enumerate(df.iter_rows()):
            if (
                "REF" in row
                and "TRANSACTION DATE" in row
                and "POSTED DATE" in row
                and "TYPE" in row
                and "DESCRIPTION" in row
                and "Category" in row
                and "AMOUNT" in row
            ):
                header_row = i
                break

        if header_row is None:
            raise ValueError(
                "Could not find header row with 'REF', 'TRANSACTION DATE', 'POSTED DATE', 'TYPE', 'DESCRIPTION', 'Category', 'AMOUNT'"
            )

        # Filter rows after the header row and drop unnecessary columns
        df1 = (
            df.with_row_index()
            .filter(pl.col("index") > header_row)
            .drop("index", "REF", "POSTED DATE", "TYPE", "Category")
        )

        # Rename columns to more normalized names
        df2 = df1.rename(
            {
                "TRANSACTION DATE": "date",
                "DESCRIPTION": "merchant",
                "AMOUNT": "cost",
            }
        )

        # Convert date strings to date objects
        df3 = df2.with_columns(pl.col("date").str.to_date(format="%d-%m-%Y"))

        # Convert cost strings to decimal numbers
        df4 = df3.with_columns(pl.col("cost").str.to_decimal())

        # Filter out rows where cost is negative (we only want expenses)
        df5 = df4.filter(pl.col("cost") > 0)

        self.df = df5

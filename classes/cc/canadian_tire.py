import polars as pl

from classes.cc.generics.file_based_card_statement import FileBasedCardStatement


class CanadianTireStatement(FileBasedCardStatement):
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
        df1 = df.with_row_index().filter(pl.col("index") > header_row)

        df1 = (
            df.with_row_index()
            .filter(pl.col("index") > header_row)
            .drop("index", "column_3")
        )

        # Filter out rows whose type is payment
        df2 = df1.filter(pl.col("column_4") != "PAYMENT")

        # Rename columns to more normalized names
        df3 = df2.rename(
            {
                "column_2": "date",
                "column_5": "merchant",
                "column_7": "cost",
            }
        )

        # Convert date strings to date objects
        df4 = df3.with_columns(pl.col("date").str.to_date(format="%Y-%m-%d"))

        # Convert cost strings to decimal numbers
        df5 = df4.with_columns(pl.col("cost").str.to_decimal())

        # add cc_category as None
        df6 = df5.with_columns(pl.lit(None).alias("cc_category"))

        self.df = df6

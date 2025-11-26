import polars as pl

from classes.cc.generics.file_based_card_statement import FileBasedCardStatement


class TdDebitStatement(FileBasedCardStatement):
    def __init__(self, file_path: str):
        super().__init__(type="td_debit", file_path=file_path)

    def load_data(self) -> None:
        """
        Load and process TD Debit transaction data from a CSV file.

        This function reads a CSV file containing TD Debit transaction data and transforms
        it into a standardized format for database insertion.

        The CSV format from TD is:
        - Column 1: Transaction Date (YYYY-MM-DD)
        - Column 2: Merchant Description
        - Column 3: Debit Amount (withdrawals/expenses)
        - Column 4: Credit Amount (deposits/income)
        - Column 5: Balance

        Args:
            file_path: Path to the CSV file containing TD Debit transaction data

        Returns:
            pl.DataFrame: Processed DataFrame with standardized column names and data types

        Raises:
            Exception: If the file cannot be read or processed
        """
        # Read the CSV file without headers (TD CSVs have no header row)
        df = pl.read_csv(source=self.file_path, has_header=False)

        # Rename columns to normalized names
        df1 = df.rename(
            {
                "column_1": "date",
                "column_2": "merchant",
                "column_3": "credit",
                "column_4": "debit",
                "column_5": "balance",
            }
        )

        # Convert date strings to date objects (already in YYYY-MM-DD format)
        df2 = df1.with_columns(pl.col("date").str.to_date(format="%Y-%m-%d"))

        # Make debit amounts negative and coalesce with credits (debits are expenses, credits are income)
        df3 = df2.with_columns(
            pl.coalesce([pl.col("credit"), -pl.col("debit")]).alias("cost")
        )

        # Define list of merchant patterns to skip (transfers, fees, mortgage, etc.)
        skip_merchants = [
            "TFR-TO",  # Transfers to other accounts
            "TFR-FR",  # Transfers from other accounts
            # "SEND E-TFR",  # E-transfers sent
            "E-TRANSFER",  # E-transfers received (in debit column when reversed)
            "PYT TO:",  # Bill payments
            # "TD MORTGAGE",  # Mortgage payments
            # "TD ATM W/D",  # ATM withdrawals
            # "CASH WITHDRAWAL",  # Cash withdrawals
            # "OVERDRAFT INTEREST",  # Bank fees
            # "O.D.P. FEE",  # Overdraft protection fee
            # "MONTHLY ACCOUNT FEE",  # Monthly fees
            # "FEE REBATE",  # Fee rebates
            # "NSF PAID FEE",  # NSF fees
            "MOBILE DEPOSIT",  # Mobile deposits
            "RICHMOND HILL C  PAY",
            "MERRY ELECTRONI  PAY",
            "CDACARBONREBATE",
            "MANULIFE 729642  HDC",
            "IG FIN SER SFGI  INV",
            "ROGRS BNK MC",
            "CAN TIRE MC",
            "CIBC MC",
        ]

        # Filter out transactions from skip list
        df4 = df3
        for merchant in skip_merchants:
            df4 = df4.filter(
                ~(pl.col("merchant").str.to_uppercase().str.contains(merchant.upper()))
            )

        # Add cc_category as None
        df5 = df4.with_columns(pl.lit(None).alias("cc_category"))

        # Select only the columns we need
        df6 = df5.select(["date", "merchant", "cost", "cc_category"])

        self.df = df6

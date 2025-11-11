import polars as pl

from classes.cc.generics.online_card_statement import OnlineCardStatement
from wealthsimpleton import wealthsimpleton as ws


class WealthsimpleDebitStatement(OnlineCardStatement):
    def __init__(self):
        super().__init__(type="ws_debit")

    def load_data(self) -> None:
        """
        Load and process Wealthsimple transaction data from a CSV file.

        This function reads a CSV file containing Wealthsimple transaction data, identifies
        the header row, extracts relevant columns, and transforms the data into a
        standardized format for database insertion.

        Args:
            file_path: Path to the CSV file containing Wealthsimple transaction data

        Returns:
            pl.DataFrame: Processed DataFrame with standardized column names and data types

        Raises:
            ValueError: If the file doesn't exist or required headers can't be found
        """
        transactions: list[dict] = ws.get_transactions(
            account_activity_url_suffix=self.config.ws_debt_link
        )
        df = pl.DataFrame(transactions)
        df1 = df.filter(
            ~(
                pl.col("type").is_in(
                    [
                        "Chequing",
                        "Visa Infinite",
                        "Direct deposit",
                        "Electronic funds transfer",
                    ]
                )
            )
        )

        # in pre-authorized debit, ignore AMEX BILL PYMT
        # in bill pay, ignore BMO MASTERCARD and ROGERS BANK-MASTERCARD
        # ignore Interac e-Transfer: Nathan Li Simplii
        df2 = df1.filter(
            ~(
                (
                    (pl.col("type") == "Pre-authorized debit")
                    & (
                        pl.col("description").is_in(
                            ["AMEX BILL PYMT", "Coinbase", "CDN TIRE"]
                        )
                    )
                )
                | (
                    (pl.col("type") == "Bill pay")
                    & (
                        pl.col("description").is_in(
                            [
                                "BMO MASTERCARD",
                                "ROGERS BANK-MASTERCARD",
                                "VISA ROYAL BANK",
                                "SIMPLII FINANCIAL CASH BACK VISA",
                                "Triangle MC",
                                "BRIM FINANCIAL",
                                "Amazon MBNA",
                            ]
                        )
                    )
                )
                | (
                    (pl.col("type") == "Interac e-Transfer")
                    & (
                        pl.col("description").is_in(
                            [
                                "Nathan Li Simplii",
                                "Nathan Li",
                                "NATHAN CHI CHUNG LI",
                                "NDAX PAYMENT",
                                "KIT MEI TONG",
                                "Nathan Li EQ Bank",
                                "Simplii Nathan",
                            ]
                        )
                    )
                )
                | (
                    (pl.col("type") == "Credit card payment")
                    & (pl.col("description") == "Wealthsimple credit card")
                )
            )
        )

        # merge description & type
        df3 = df2.with_columns(
            pl.concat_str(
                [pl.col("type"), pl.col("description")], separator=": "
            ).alias("merchant")
        )

        # Rename columns to more normalized names
        df4 = df3.rename(
            {
                "amount": "cost",
            }
        )
        # Add a dummy cc_category column with None values
        df5 = df4.with_columns(pl.lit(None).alias("cc_category"))

        # Convert date strings to date objects
        df6 = df5.with_columns(pl.col("date").str.to_date(format="%Y-%m-%dT%H:%M:%S"))

        # parse float values from after the $ sign
        df7 = df6.with_columns(
            pl.col("cost")
            .str.replace_all("−", "-")  # normalize Unicode minus to ASCII
            .str.replace_all(",", "")  # strip commas
            .str.replace_all(r"[^\d.\-]", "")  # keep only digits, dot, minus
            .cast(pl.Float64)
            .alias("new_cost")
        )

        # multiple new_cost by -1
        df8 = df7.with_columns(pl.col("new_cost").mul(-1))

        df9 = df8.select(
            pl.col("date"),
            pl.col("merchant"),
            pl.col("new_cost").alias("cost"),
            pl.col("cc_category"),
        )

        self.df = df9

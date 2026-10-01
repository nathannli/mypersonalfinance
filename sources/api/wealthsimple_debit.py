import polars as pl

from sources.base import FileBasedCardStatement


class WealthsimpleDebitStatement(FileBasedCardStatement):
    def __init__(self, file_path: str):
        super().__init__(type="ws_debit", file_path=file_path)

    def load_data(self) -> None:
        """
        Load Wealthsimple chequing activity scraped by
        scripts/download_wealthsimple_transactions.py.

        The CSV holds the raw activity fields description, type, amount, and
        date; this transforms them into the standardized format for database
        insertion.
        """
        df = pl.read_csv(
            self.file_path,
            schema={
                "description": pl.Utf8,
                "type": pl.Utf8,
                "amount": pl.Utf8,
                "date": pl.Utf8,
            },
        )
        # Transfers and interest carry the account label ("Chequing • <nickname>")
        # where other rows carry their type.
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
                | pl.col("type").str.starts_with("Chequing • ")
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
                            ["AMEX BILL PYMT", "Coinbase", "CDN TIRE", "AMEX"]
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
        df6 = df5.with_columns(pl.col("date").str.to_date(format="%Y-%m-%d"))

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

        print("================================================")
        print("load data end from wealthsimple_debit.py")
        print(f"{df9=}")

        self.df = df9

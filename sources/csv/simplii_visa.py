import polars as pl

from sources.base import FileBasedCardStatement
from sources.ref_data import simplii_visa_cc_merchant_name_to_category_ref


# Credits the card issuer posts that are not merchant refunds: a payment
# against the card, and a miscellaneous account credit. Both labels were
# observed verbatim in an authenticated export (R17); a merchant credit carries
# the merchant's own name instead, so anything not matching here is one.
NON_REFUND_CREDIT_MARKERS = (
    "payment thank you",
    "paiemen t merci",
    "misc credit",
)


def _money(column: str) -> pl.Expr:
    """Normalize an amount column to a number.

    Exports differ: the authenticated one writes amounts as bare decimals,
    while spaced variants carry padding and thousands separators. Read
    everything as text and normalize explicitly, so the result never depends
    on type inference. An absent amount becomes null; an unparseable one
    still raises rather than silently vanishing.
    """
    stripped = pl.col(column).str.strip_chars()
    return (
        pl.when(stripped == "")
        .then(pl.lit(None, dtype=pl.String))
        .otherwise(stripped)
        .str.replace_all(",", "")
        .cast(pl.Float64)
        .alias(column)
    )


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
        # Read as text: amount normalization below is explicit, so spaced
        # and unspaced exports parse identically (V15, B5).
        df = pl.read_csv(source=self.file_path, has_header=True, infer_schema_length=0)

        # The export's header labels carry stray spaces that vary between
        # exports, so address them by their stripped names.
        df1 = df.rename({name: name.strip() for name in df.columns})

        # Rename columns to more normalized names
        df2 = df1.rename(
            {
                "Date": "date",
                "Transaction Details": "merchant",
                "Funds Out": "funds_out",
                "Funds In": "funds_in",
            }
        )
        # Add a dummy cc_category column with None values
        df3 = df2.with_columns(
            pl.lit(None).alias("cc_category"),
            _money("funds_out"),
            _money("funds_in"),
        )

        # Convert date strings to date objects
        df4 = df3.with_columns(pl.col("date").str.to_date(format="%m/%d/%Y"))

        # A purchase is funds out. A merchant credit is funds in and becomes a
        # negative cost, so a refund reduces the month's spending instead of
        # vanishing; card payments and bank credits are not expenses and stay
        # dropped (V15, B5).
        merchant_credit = (
            pl.col("funds_in").is_not_null() & pl.col("funds_out").is_null()
        )
        for marker in NON_REFUND_CREDIT_MARKERS:
            merchant_credit = merchant_credit & ~(
                pl.col("merchant").str.to_lowercase().str.contains(marker)
            )
        df5 = df4.with_columns(
            pl.when(pl.col("funds_out").is_not_null())
            .then(pl.col("funds_out"))
            .when(merchant_credit)
            .then(-pl.col("funds_in"))
            .otherwise(None)
            .alias("cost")
        )

        # The card export also carries a Credit Card column; keep only the
        # standardized set the registry promises (V5).
        self.df = df5.filter(pl.col("cost").is_not_null()).select(
            "date", "merchant", "cost", "cc_category"
        )

    @staticmethod
    def auto_match_category() -> tuple[str, str]:
        """
        This is static because this card is only used for restaurants.
        """
        return simplii_visa_cc_merchant_name_to_category_ref["Restaurants"]

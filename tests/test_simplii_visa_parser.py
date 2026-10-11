"""Parser-level behavior for the Simplii Visa statement.

The fixture's first five data rows come verbatim from an authenticated card
export (R17): two purchases, two card payments and one miscellaneous credit.
The final two rows are constructed merchant credits, because no real refund
has been observed in an export yet (B5).
"""

import unittest
from datetime import date
from pathlib import Path

from sources.csv.simplii_visa import SimpliiVisaStatement

FIXTURE = Path(__file__).parent / "fixtures" / "simplii_visa_credits.csv"


class TestSimpliiVisaStatement(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.df = SimpliiVisaStatement(str(FIXTURE)).get_df()

    def rows(self) -> list[dict]:
        return self.df.iter_rows(named=True)

    def test_standardized_columns(self) -> None:
        self.assertEqual(self.df.columns, ["date", "merchant", "cost", "cc_category"])
        self.assertTrue(self.df["cc_category"].null_count() == self.df.height)

    def test_purchases_keep_positive_cost(self) -> None:
        purchases = {
            (row["merchant"], row["cost"]) for row in self.rows() if row["cost"] > 0
        }
        self.assertEqual(
            purchases,
            {
                ("TIGER SUGAR TORONTO, ON", 18.31),
                ("FORMOCHA TORONTO, ON", 16.27),
            },
        )

    def test_card_payments_are_dropped(self) -> None:
        merchants = [row["merchant"] for row in self.rows()]
        self.assertFalse(any("PAYMENT THANK YOU" in name for name in merchants))
        self.assertFalse(any("PAIEMEN T MERCI" in name for name in merchants))

    def test_miscellaneous_credit_is_dropped(self) -> None:
        merchants = [row["merchant"] for row in self.rows()]
        self.assertNotIn("MISC CREDIT", merchants)

    def test_merchant_credit_becomes_negative_cost(self) -> None:
        credits = {
            (row["merchant"], row["cost"]) for row in self.rows() if row["cost"] < 0
        }
        self.assertEqual(
            credits,
            {
                ("REFUND TIGER SUGAR TORONTO, ON", -18.31),
                ("FORMOCHA TORONTO, ON", -16.27),
            },
        )

    def test_dates_parse_as_dates(self) -> None:
        self.assertTrue(all(isinstance(row["date"], date) for row in self.rows()))


if __name__ == "__main__":
    unittest.main()

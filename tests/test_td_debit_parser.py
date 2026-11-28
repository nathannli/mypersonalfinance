import tempfile
import unittest
from pathlib import Path

import polars as pl

from sources.csv.td_debit import TdDebitStatement


class TdDebitEmptyColumnTest(unittest.TestCase):
    def parse(self, text: str) -> pl.DataFrame:
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "td.csv"
            path.write_text(text)
            return TdDebitStatement(str(path)).get_df()

    def test_withdrawals_only(self):
        df = self.parse("2026-11-01,SHOP,10.00,,100.00\n")
        self.assertEqual(df["cost"].to_list(), [10.0])
        self.assertTrue(df["cost"].dtype.is_numeric())

    def test_deposits_only(self):
        df = self.parse("2026-11-01,SHOP REFUND,,10.50,100.00\n")
        self.assertEqual(df["cost"].to_list(), [-10.5])
        self.assertTrue(df["cost"].dtype.is_numeric())


if __name__ == "__main__":
    unittest.main()

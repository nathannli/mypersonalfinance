"""
Test script: fetch and print SimplyFIN Bridge transactions without DB writes.

Usage:
    uv run python scripts/test_simplefin.py
"""

import os
import sys

# Add project root to sys.path so `sources` package resolves
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import polars as pl

from sources.api.simplefin import SimplefinStatement


def main():
    statement = SimplefinStatement()
    df = statement.get_df()

    pl.Config.set_tbl_cols(20)
    pl.Config.set_tbl_rows(100)
    pl.Config.set_fmt_str_lengths(1000)

    if df.is_empty():
        print("No transactions returned.")
        return

    print(f"\n{df.height} transactions fetched:\n")
    print(df)


if __name__ == "__main__":
    main()

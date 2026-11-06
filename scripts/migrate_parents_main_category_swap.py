"""Swap `parents_finance.main_category` ids to match `finance.expense_type`.

Before: (1, 'Fixed'), (2, 'Variable'). After: (1, 'variable'), (2, 'fixed').
Only `categories.main_category_id` flips (1 <-> 2) and the two `main_category`
rows are renamed. `expenses` reference `categories.id`, so no expense row
changes.

Every precondition and postcondition is asserted inside one transaction: the
bucket each category belongs to must be the same before and after, and the
`expenses` row count, total cost and per-category counts must not move.

Take a backup first:
    pg_dump "$POSTGRES_CONNECTION_STRING/parents_finance" > /path/outside/repo.sql

Usage:
    uv run --frozen python scripts/migrate_parents_main_category_swap.py --dry-run
    uv run --frozen python scripts/migrate_parents_main_category_swap.py
"""

from __future__ import annotations

import argparse
import sys

import psycopg

from config import Config

DATABASE = "parents_finance"
OLD_ROWS = [(1, "Fixed"), (2, "Variable")]
NEW_ROWS = [(1, "variable"), (2, "fixed")]


def bucket_snapshot(cur) -> dict[int, str]:
    cur.execute(
        "select c.id, lower(m.name) from categories c "
        "join main_category m on m.id = c.main_category_id order by c.id"
    )
    return {row[0]: row[1] for row in cur.fetchall()}


def expense_snapshot(cur) -> tuple:
    cur.execute("select count(*), coalesce(sum(cost), 0) from expenses")
    total = cur.fetchone()
    cur.execute("select category_id, count(*) from expenses group by 1 order by 1")
    return total, cur.fetchall()


def migrate(dry_run: bool) -> int:
    conn = psycopg.connect(
        f"{Config(debug=False).postgres_connection_string}/{DATABASE}"
    )
    with conn, conn.cursor() as cur:
        cur.execute("select id, name from main_category order by id")
        current = cur.fetchall()
        if current == NEW_ROWS:
            print(f"already applied: main_category is {current}")
            print("nothing written")
            return 0
        if current != OLD_ROWS:
            sys.exit(f"main_category is {current}, expected {OLD_ROWS}; aborting")

        before_buckets = bucket_snapshot(cur)
        before_expenses = expense_snapshot(cur)
        print(f"categories: {len(before_buckets)}")
        print(f"expenses: {before_expenses[0][0]} rows, cost {before_expenses[0][1]}")

        cur.execute("update categories set main_category_id = 3 - main_category_id")
        cur.execute(
            "update main_category set name = "
            "case id when 1 then 'variable' else 'fixed' end"
        )

        cur.execute("select id, name from main_category order by id")
        after_rows = cur.fetchall()
        after_buckets = bucket_snapshot(cur)
        after_expenses = expense_snapshot(cur)
        print(f"main_category {current} -> {after_rows}")
        if after_rows != NEW_ROWS:
            sys.exit("main_category postcondition failed; rolled back")
        if after_buckets != before_buckets:
            sys.exit("a category changed bucket; rolled back")
        if after_expenses != before_expenses:
            sys.exit("expenses changed; rolled back")
        print("bucket per category unchanged (by name), expenses unchanged")

        if dry_run:
            conn.rollback()
            print("dry run, nothing written")
        else:
            print("applied")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Run every check inside the transaction, then roll back.",
    )
    args = parser.parse_args(argv)
    return migrate(args.dry_run)


if __name__ == "__main__":
    sys.exit(main())

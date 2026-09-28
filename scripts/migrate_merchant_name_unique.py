"""T9: replace the 3-column UNIQUE on `finance.merchant_name_auto_match`.

The table carried `UNIQUE (merchant_name, merchant_category,
merchant_subcategory)`, which permits the same merchant to be listed twice
with two different categories. The lookup that reads this table is a lookup by
merchant, so two rows for one merchant is not a supported state -- it means the
table was edited row by row and nothing stopped the second edit. The UNIQUE
becomes `UNIQUE (merchant_name)`, so a second row for a merchant cannot be
written at all.

164 rows are already checked, and no merchant appears twice, so the narrower
constraint is accepted without deleting or merging anything. Every precondition
is asserted inside the transaction, so a table that has drifted since the T20
backup aborts the change instead of applying it to a different state than the
one that was backed up.

Take the backup first:
    uv run --frozen python scripts/backup_finance_auto_match.py

Usage:
    uv run --frozen python scripts/migrate_merchant_name_unique.py --dry-run
    uv run --frozen python scripts/migrate_merchant_name_unique.py
"""

from __future__ import annotations

import argparse
import sys

import psycopg

from config import Config

DATABASE = "finance"
TABLE = "merchant_name_auto_match"
CONSTRAINT = "merchant_name_auto_match_merchant_name_merchant_category_key"

EXPECTED_ROWS = 164
OLD_DEF = "UNIQUE (merchant_name, merchant_category, merchant_subcategory)"
NEW_DEF = "UNIQUE (merchant_name)"


def migrate(dry_run: bool) -> int:
    # Explicit transaction, never autocommit. DDL and its postcondition must
    # land together: a committed DROP with a failed ADD would leave the table
    # with no UNIQUE at all, silently accepting the duplicates this change
    # exists to prevent.
    conn = psycopg.connect(
        f"{Config(debug=False).postgres_connection_string}/{DATABASE}"
    )
    with conn, conn.cursor() as cur:
        cur.execute(f"select count(*) from {TABLE}")
        rows = cur.fetchone()[0]
        if rows != EXPECTED_ROWS:
            sys.exit(f"expected {EXPECTED_ROWS} rows, found {rows}; re-take the backup")

        cur.execute(
            "select pg_get_constraintdef(oid) from pg_constraint "
            "where conrelid = %s::regclass and conname = %s",
            (TABLE, CONSTRAINT),
        )
        found = cur.fetchone()
        if found is None:
            sys.exit(f"constraint {CONSTRAINT} is missing; re-take the backup")
        current_def = found[0]
        if current_def == NEW_DEF:
            print(f"already applied: {CONSTRAINT} is {current_def}")
            print("nothing written")
            return 0
        if current_def != OLD_DEF:
            sys.exit(f"{CONSTRAINT} is {current_def!r}, expected {OLD_DEF!r}; aborting")

        # Precondition: no merchant carries two categories, or the narrower
        # UNIQUE cannot be created at all.
        cur.execute(
            f"select merchant_name, count(*) from {TABLE} group by 1 having count(*) > 1"
        )
        duplicates = cur.fetchall()
        if duplicates:
            sys.exit(f"merchants carry more than one row: {duplicates}; aborting")

        if dry_run:
            print(f"dry run, no writes. {rows} rows, 0 duplicate merchants.")
            print(f"  would replace {CONSTRAINT}")
            print(f"    {current_def}")
            print(f"    -> {NEW_DEF}")
            return 0

        cur.execute(f"alter table {TABLE} drop constraint {CONSTRAINT}")
        cur.execute(
            f"alter table {TABLE} add constraint {CONSTRAINT} unique (merchant_name)"
        )

        # Postconditions, verified inside the same transaction.
        cur.execute(f"select count(*) from {TABLE}")
        after_rows = cur.fetchone()[0]
        cur.execute(
            "select pg_get_constraintdef(oid) from pg_constraint "
            "where conrelid = %s::regclass and conname = %s",
            (TABLE, CONSTRAINT),
        )
        after_def = cur.fetchone()[0]
        cur.execute(
            f"select count(*) from {TABLE} where merchant_name is null or merchant_name = ''"
        )
        empty_names = cur.fetchone()[0]

        print(f"rows {rows} -> {after_rows}")
        print(f"{CONSTRAINT}")
        print(f"  {current_def}")
        print(f"  {after_def}")
        if after_rows != rows or after_def != NEW_DEF:
            sys.exit("postcondition failed; transaction rolled back")
        if empty_names:
            sys.exit(f"{empty_names} blank merchant_name; transaction rolled back")
        print("blank merchant_name: 0")

        # `with conn` commits on a clean exit and rolls back when an exception
        # propagates, so sys.exit() above is the abort path and there is
        # nothing to commit here.
        if dry_run:
            conn.rollback()
            print("dry run, nothing written")
        else:
            print("T9 applied")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print the change and check every precondition. Write nothing.",
    )
    args = parser.parse_args(argv)
    return migrate(args.dry_run)


if __name__ == "__main__":
    sys.exit(main())

"""T10: repair the one `finance.substring_auto_match` rule that can never fire.

Id 78, `paula's choice`, names `('Shopping', 'Hygiene')`. `Hygiene` is
subcategory 23 and sits under `Personal Care`, so the pair does not exist and
`find_reference_choice` returns None for it: the rule silently never fires, and
nothing reports it. That is the whole reason V9 exists.

The target is not a guess. Every other `Hygiene` row in the table names
`Personal Care` -- all 4 of them -- and every other `Personal Care` row names a
merchant in the same family (pharmacies, salons, cosmetics, supplements).
`paula's choice` is a cosmetics brand, so `('Personal Care', 'Hygiene')` is
the pair the rest of the table already asserts.

Every precondition is asserted inside the transaction, so a table that has
drifted since the T20 backup aborts the repair instead of applying it to a
different state than the one that was backed up.

Take the backup first:
    uv run --frozen python scripts/backup_finance_auto_match.py

Usage:
    uv run --frozen python scripts/repair_substring_auto_match.py --dry-run
    uv run --frozen python scripts/repair_substring_auto_match.py
"""

from __future__ import annotations

import argparse
import sys

import psycopg

from config import Config

DATABASE = "finance"
TABLE = "substring_auto_match"
ROW_ID = 78

EXPECTED_ROWS = 100
EXPECTED_BEFORE = ("paula's choice", "Shopping", "Hygiene")
REPAIR = ("paula's choice", "Personal Care", "Hygiene")
# merchant_category is the only column that changes.
REPAIR_CATEGORY = "Personal Care"

# The live pair, asserted to exist before the write and again after it.
TARGET_SUBCATEGORY = "Hygiene"


def orphans(cur: psycopg.Cursor) -> list[tuple]:
    """Rows naming a category/subcategory pair the live taxonomy lacks."""
    cur.execute(
        f"select s2.id, s2.substring, s2.merchant_category, s2.merchant_subcategory "
        f"from {TABLE} s2 where not exists ("
        "select 1 from subcategories s join categories c on c.id=s.category_id "
        "where s.name=s2.merchant_subcategory and c.name=s2.merchant_category) "
        "order by s2.id"
    )
    return cur.fetchall()


def repair(dry_run: bool) -> int:
    # Explicit transaction, never autocommit. A scratch run of the parents
    # repair caught the bug autocommit creates: the delete committed, then the
    # postcondition failed, and the table was left half-repaired. The commit
    # below is the only commit, and it is after every check.
    conn = psycopg.connect(
        f"{Config(debug=False).postgres_connection_string}/{DATABASE}"
    )
    with conn, conn.cursor() as cur:
        # Precondition 1: the target pair is live. Repairing to a pair that
        # does not exist would replace one dead rule with another.
        cur.execute(
            "select c.name from subcategories s join categories c on c.id=s.category_id "
            "where s.name = %s",
            (TARGET_SUBCATEGORY,),
        )
        parent = cur.fetchall()
        if parent != [(REPAIR_CATEGORY,)]:
            sys.exit(
                f"{TARGET_SUBCATEGORY!r} sits under {parent}, not "
                f"{(REPAIR_CATEGORY,)}; the target moved, aborting"
            )

        cur.execute(f"select count(*) from {TABLE}")
        rows = cur.fetchone()[0]
        if rows != EXPECTED_ROWS:
            sys.exit(f"expected {EXPECTED_ROWS} rows, found {rows}; re-take the backup")

        cur.execute(
            f"select substring, merchant_category, merchant_subcategory "
            f"from {TABLE} where id = %s",
            (ROW_ID,),
        )
        found = cur.fetchone()
        if found is None:
            sys.exit(f"id {ROW_ID} is missing; re-take the backup")
        if found == REPAIR:
            print(f"already repaired: id {ROW_ID} is {found}")
            print("nothing written")
            return 0
        if found != EXPECTED_BEFORE:
            sys.exit(f"id {ROW_ID} is {found}, expected {EXPECTED_BEFORE}; aborting")

        # Precondition 2: id 78 is the only rule that cannot resolve. A second
        # one would mean the defect is wider than the single row T10 repairs.
        broken = orphans(cur)
        if broken != [(ROW_ID, *EXPECTED_BEFORE)]:
            sys.exit(f"unresolvable rules changed: {broken}; aborting")

        # Precondition 3: no other row claims this substring, so the repair
        # cannot collide with the 3-column UNIQUE on an unexpected pair.
        cur.execute(
            f"select id, merchant_category, merchant_subcategory from {TABLE} "
            f"where substring = %s and id <> %s",
            (REPAIR[0], ROW_ID),
        )
        rivals = cur.fetchall()
        if rivals:
            sys.exit(f"other rows already claim {REPAIR[0]!r}: {rivals}; aborting")

        if dry_run:
            print(f"dry run, no writes. {rows} rows, 1 unresolvable rule.")
            print(f"  would retarget id {ROW_ID}")
            print(f"    {found}")
            print(f"    -> {REPAIR}")
            return 0

        cur.execute(
            f"update {TABLE} set merchant_category = %s where id = %s",
            (REPAIR_CATEGORY, ROW_ID),
        )
        if cur.rowcount != 1:
            sys.exit(f"id {ROW_ID}: updated {cur.rowcount} rows, expected 1; aborting")

        # Postconditions, verified inside the same transaction.
        cur.execute(
            f"select substring, merchant_category, merchant_subcategory "
            f"from {TABLE} where id = %s",
            (ROW_ID,),
        )
        after = cur.fetchone()
        cur.execute(f"select count(*) from {TABLE}")
        after_rows = cur.fetchone()[0]
        still_broken = orphans(cur)

        print(f"rows {rows} -> {after_rows}")
        print(f"id {ROW_ID}")
        print(f"  {found}")
        print(f"  {after}")
        print(f"unresolvable rules: {len(still_broken)} {still_broken}")
        if after != REPAIR or after_rows != rows or still_broken:
            sys.exit("postcondition failed; transaction rolled back")

        # `with conn` commits on a clean exit and rolls back when an exception
        # propagates, so sys.exit() above is the abort path and there is
        # nothing to commit here. The dry run returns before reaching this
        # point, having written nothing.
        print("T10 applied")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print the repair and check every precondition. Write nothing.",
    )
    args = parser.parse_args(argv)
    return repair(args.dry_run)


if __name__ == "__main__":
    sys.exit(main())

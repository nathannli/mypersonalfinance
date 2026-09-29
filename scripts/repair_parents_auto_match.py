"""T24: repair the `parents_finance.auto_match` rows that can never resolve.

Two defects, both found by T15's check and by resolving those merchants
through the real code path:

1. Five rows name a category that does not exist: `Grocery` (id 1) and
   `Interest` (ids 51, 147, 149, 150). The targets are in the data rather
   than guessed -- parents substring rule 19 already maps `costco` to `Food`,
   and every one of those three `Interest` merchants moved from `Loans` to
   `Fees & Interest` in mid-2025 with no row going back.
2. `PROMO INTEREST` holds two exact rows, id 150 to the dead `Interest` and
   id 202 to the live `Fees & Interest`. `get_auto_match_category` raises on
   two rows, so the one merchant with a correct mapping is the one rejected.

Id 150 is deleted rather than retargeted, because id 202 already holds the
correct live value.

Every precondition is asserted inside the transaction, so a live table that
has drifted since the backup aborts the whole repair instead of applying
half of it.

Usage:
    uv run --frozen python scripts/repair_parents_auto_match.py --dry-run
    uv run --frozen python scripts/repair_parents_auto_match.py
"""

from __future__ import annotations

import argparse
import sys

import psycopg

from config import Config

DATABASE = "parents_finance"
TABLE = "auto_match"

# merchant_category is the only column that changes. Row 150 is deleted
# outright because row 202 already carries the correct live value.
REPAIR = {
    1: "Food",
    51: "Fees & Interest",
    147: "Fees & Interest",
    149: "Fees & Interest",
}
DELETE_ID = 150

# Exactly the state T15 pinned, asserted before anything is written.
EXPECTED_DEAD = {
    1: "Grocery",
    51: "Interest",
    147: "Interest",
    149: "Interest",
    150: "Interest",
}


def repair(dry_run: bool) -> int:
    # Explicit transaction, never autocommit. A scratch run of this script
    # caught the bug that autocommit creates: the delete committed, then the
    # postcondition failed, and the table was left half-repaired with no way
    # back. The commit below is the only commit, and it is after every check.
    conn = psycopg.connect(
        f"{Config(debug=False).postgres_connection_string}/{DATABASE}"
    )
    with conn, conn.cursor() as cur:
        cur.execute("select id, name from categories")
        live_categories = {name for _, name in cur.fetchall()}
        if "Food" not in live_categories or "Fees & Interest" not in live_categories:
            sys.exit("target categories are not live; aborting")

        cur.execute(
            "select id, merchant_name, merchant_category from auto_match order by id"
        )
        rows = cur.fetchall()
        before = len(rows)
        if before != 204:
            sys.exit(f"expected 204 rows, found {before}; re-take the backup")

        # Precondition 1: the rows named in EXPECTED_DEAD say what we recorded.
        current = {rid: cat for rid, _, cat in rows}
        for rid, cat in EXPECTED_DEAD.items():
            if current.get(rid) != cat:
                sys.exit(
                    f"id {rid} is {current.get(rid)!r}, expected {cat!r}; aborting"
                )

        # Precondition 2: every dead-category row is one we know about.
        dead = {rid: cat for rid, cat in current.items() if cat not in live_categories}
        if dead != EXPECTED_DEAD:
            sys.exit(f"dead-category rows changed: {dead}; aborting")

        # Precondition 3: exactly one duplicate merchant, the one we expect.
        seen: dict[str, list[int]] = {}
        for rid, merchant, _ in rows:
            seen.setdefault(merchant, []).append(rid)
        dups = {m: ids for m, ids in seen.items() if len(ids) > 1}
        if dups != {"PROMO INTEREST": [150, 202]}:
            sys.exit(f"duplicate merchants changed: {dups}; aborting")

        if dry_run:
            print(f"dry run, no writes. {before} rows.")
            print(f"  would retarget {len(REPAIR)} row(s) and delete id {DELETE_ID}")
            for rid, cat in sorted(REPAIR.items()):
                print(f"    id {rid}: {current[rid]!r} -> {cat!r}")
            print(f"    id {DELETE_ID}: {current[DELETE_ID]!r} deleted")
            return 0

        for rid, cat in sorted(REPAIR.items()):
            cur.execute(
                f"update {TABLE} set merchant_category = %s where id = %s", (cat, rid)
            )
            if cur.rowcount != 1:
                sys.exit(f"id {rid}: updated {cur.rowcount} rows, expected 1; aborting")
        cur.execute(f"delete from {TABLE} where id = %s", (DELETE_ID,))
        if cur.rowcount != 1:
            sys.exit(
                f"id {DELETE_ID}: deleted {cur.rowcount} rows, expected 1; aborting"
            )

        # Postconditions, verified inside the same transaction.
        cur.execute(
            "select id, merchant_name, merchant_category from auto_match order by id"
        )
        after = cur.fetchall()
        cur.execute(
            "select merchant_name from auto_match group by 1 having count(*) > 1"
        )
        remaining_dups = cur.fetchall()
        still_dead = [r for r in after if r[2] not in live_categories]

        print(f"rows {before} -> {len(after)}")
        print(f"rows naming a non-live category: {len(still_dead)} {still_dead}")
        print(f"duplicate merchants: {remaining_dups}")
        if still_dead or remaining_dups:
            sys.exit("postcondition failed; transaction rolled back")

        cur.execute(
            f"select setval(pg_get_serial_sequence('{TABLE}', 'id'), (select max(id) from {TABLE}))"
        )
        print(f"sequence -> {cur.fetchone()[0]}")

        # `with conn` commits on a clean exit and rolls back when an exception
        # propagates, so sys.exit() above is the abort path and there is
        # nothing to commit here. An explicit commit outside the block raised
        # "the connection is closed" on the first real run, after the repair
        # had already landed. Dry run rolls back from inside, because a clean
        # exit would otherwise commit.
        if dry_run:
            conn.rollback()
            print("dry run, nothing written")
        else:
            print("T24 applied")
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

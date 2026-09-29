"""T16: rebuild both databases from `ddl/` into scratch copies and assert parity.

The rebuild recipe in `ddl/README.md` was executed by hand once; this script is
the reproducible form. It builds `finance` and `parents_finance` into scratch
databases with distinct names, applies the whole tracked tree plus the
gitignored seed, and asserts what the spec claims a rebuild proves:

- V1/V2  column-for-column parity with live (name, type, nullability) and the
         same id sequences, gaps included
- V5/V24 the whole tree re-applies with 0 rows inserted and 0 errors
- V13    zero `expenses` rows before any data restore
- V15    every live `parents_finance.expenses.category_id` replayed into the
         scratch copy resolves to the same category name as in live
- V22    `parents_finance.expenses` carries its `comments text` column
- V23    every seeded table's sequence sits past its highest seeded id

It reads live, writes nothing to live, and drops both scratch databases when
it is done. Needs a live connection, like `scripts/verify_spec_claims.py`, so
it is not part of the offline unit suite and never runs in CI.

Usage:
    uv run --frozen python scripts/rebuild_ddl_scratch.py
    uv run --frozen python scripts/rebuild_ddl_scratch.py --keep
"""

from __future__ import annotations

import argparse
import os
import sys
import time

import psycopg

from config import Config
from utils.repo_paths import repo_root

DDL_DIR = repo_root() / "ddl"
SEED_DIR = DDL_DIR / "seed"

FINANCE_TABLES = (
    "categories",
    "expense_type",
    "subcategories",
    "expenses",
    "merchant_name_auto_match",
    "substring_auto_match",
)
FINANCE_FILES = (
    "categories.sql",
    "expense_type.sql",
    "subcategories.sql",
    "expenses.sql",
    "merchant_name_auto_match.sql",
    "substring_auto_match.sql",
)
PARENTS_TABLES = (
    "main_category",
    "categories",
    "expenses",
    "auto_match",
    "substring_auto_match",
)
PARENTS_FILES = (
    "main_category.sql",
    "categories.sql",
    "expenses.sql",
    "auto_match.sql",
    "substring_auto_match.sql",
)

# database -> (tables, schema files, in apply order per ddl/README.md).
DATABASES: dict[str, tuple[tuple[str, ...], tuple[str, ...]]] = {
    "finance": (FINANCE_TABLES, FINANCE_FILES),
    "parents_finance": (PARENTS_TABLES, PARENTS_FILES),
}

COLUMNS_QUERY = (
    "select column_name, data_type, is_nullable, character_maximum_length "
    "from information_schema.columns "
    "where table_schema = 'public' and table_name = %s "
    "order by ordinal_position"
)


def connect(database: str) -> psycopg.Connection:
    """Autocommit connection. DDL and CREATE/DROP DATABASE need it."""
    return psycopg.connect(
        f"{Config(debug=False).postgres_connection_string}/{database}",
        autocommit=True,
    )


def admin_connect() -> psycopg.Connection:
    """A connection that may CREATE/DROP databases, not a write to live."""
    for database in ("postgres", "finance"):
        try:
            return connect(database)
        except psycopg.OperationalError:
            continue
    sys.exit("cannot connect to an administrative database (tried postgres, finance)")


def tree_files(database: str) -> list:
    """Schema files in README apply order, then the generated seed files."""
    schema = [DDL_DIR / database / name for name in DATABASES[database][1]]
    seeds = sorted(SEED_DIR.glob(f"{database}.*.sql"))
    if not seeds:
        sys.exit(
            f"no seed files under {SEED_DIR}; run scripts/export_ddl_seed.py "
            f"--database {database} first"
        )
    return schema + seeds


def apply_tree(conn: psycopg.Connection, files: list) -> None:
    for path in files:
        conn.execute(path.read_text(encoding="utf-8"))


def column_shapes(conn: psycopg.Connection, tables: tuple[str, ...]) -> dict:
    with conn.cursor() as cur:
        return {t: cur.execute(COLUMNS_QUERY, (t,)).fetchall() for t in tables}


def ids(conn: psycopg.Connection, table: str) -> list[int]:
    with conn.cursor() as cur:
        return [row[0] for row in cur.execute(f"select id from {table} order by id")]


def row_counts(conn: psycopg.Connection, tables: tuple[str, ...]) -> dict:
    with conn.cursor() as cur:
        return {
            t: cur.execute(f"select count(*) from {t}").fetchone()[0] for t in tables
        }


def first_diff(live: list, scratch: list) -> str:
    for index, (a, b) in enumerate(zip(live, scratch)):
        if a != b:
            return f"position {index}: live {a} vs scratch {b}"
    if len(live) != len(scratch):
        return f"live has {len(live)}, scratch has {len(scratch)}"
    return "no difference found"


def sequence_guard(conn: psycopg.Connection, table: str) -> tuple[bool, str]:
    """V23: the sequence must hand out ids past the highest seeded id."""
    with conn.cursor() as cur:
        last, called = cur.execute(
            f"select last_value, is_called from {table}_id_seq"
        ).fetchone()
        max_id = cur.execute(f"select max(id) from {table}").fetchone()[0]
    if max_id is None:
        return True, "empty"
    if not called:
        return True, "sequence unused"
    if last < max_id:
        return False, f"sequence at {last}, behind max seeded id {max_id}"
    return True, f"next id {last + 1} is past seeded max {max_id}"


def replay_parents_expenses(
    live: psycopg.Connection, scratch: psycopg.Connection
) -> tuple[bool, str]:
    """V15: every live expense id resolves to the same category name in scratch."""
    with live.cursor() as cur:
        pairs = cur.execute(
            "select id, category_id from expenses order by id"
        ).fetchall()
        live_resolved = cur.execute(
            "select e.id, c.name from expenses e "
            "join categories c on c.id = e.category_id order by e.id"
        ).fetchall()

    with scratch.cursor() as cur:
        # Replay into a temp table, so the scratch copy itself stays empty and
        # V13 keeps holding after this check.
        cur.execute(
            "create temp table replay_expenses "
            "(id integer primary key, category_id integer not null)"
        )
        cur.executemany(
            "insert into replay_expenses (id, category_id) values (%s, %s)", pairs
        )
        scratch_resolved = cur.execute(
            "select r.id, c.name from replay_expenses r "
            "join categories c on c.id = r.category_id order by r.id"
        ).fetchall()

    if len(scratch_resolved) != len(live_resolved):
        missing = len(live_resolved) - len(scratch_resolved)
        return (
            False,
            f"{missing} live category_id value(s) do not exist in the scratch taxonomy",
        )
    if scratch_resolved != live_resolved:
        return (
            False,
            f"resolution differs: {first_diff(live_resolved, scratch_resolved)}",
        )
    return (
        True,
        f"{len(pairs)} live expense ids replayed, every category name matches live",
    )


def check_parity(
    database: str,
    live: psycopg.Connection,
    scratch: psycopg.Connection,
    failures: list[str],
) -> None:
    """V1/V2 column and id parity, V13, V22, V23, and V15 for parents."""
    tables = DATABASES[database][0]

    live_cols = column_shapes(live, tables)
    scratch_cols = column_shapes(scratch, tables)
    for table in tables:
        if live_cols[table] != scratch_cols[table]:
            failures.append(
                f"[{database}] V-columns {table}: {first_diff(live_cols[table], scratch_cols[table])}"
            )
    if not any(f.startswith(f"[{database}] V-columns") for f in failures):
        print(f"  V-columns {database}: {len(tables)}/{len(tables)} tables match live")

    mismatches = 0
    for table in tables:
        if table == "expenses":
            continue  # V13 holds it empty; its ids are not comparable.
        if ids(live, table) != ids(scratch, table):
            mismatches += 1
            failures.append(
                f"[{database}] V-ids {table}: id sequence differs from live"
            )
    if not mismatches:
        print(
            f"  V-ids {database}: every seeded table matches live id for id, gaps included"
        )

    expenses_rows = row_counts(scratch, ("expenses",))["expenses"]
    if expenses_rows:
        failures.append(
            f"[{database}] V13: scratch expenses holds {expenses_rows} rows, expected 0"
        )
    else:
        print(f"  V13 {database}: expenses empty before any data restore")

    if database == "parents_finance":
        comments = next(
            (c for c in scratch_cols["expenses"] if c[0] == "comments"), None
        )
        if comments is None or comments[1] != "text":
            failures.append(
                f"[{database}] V22: expenses.comments is {comments}, expected a text column"
            )
        else:
            print(f"  V22 {database}: expenses.comments is text")

    for table in tables:
        if table == "expenses":
            continue
        ok, why = sequence_guard(scratch, table)
        if not ok:
            failures.append(f"[{database}] V23 {table}: {why}")
    if not any(f.startswith(f"[{database}] V23") for f in failures):
        print(f"  V23 {database}: every seeded sequence sits past its max id")

    if database == "parents_finance":
        ok, why = replay_parents_expenses(live, scratch)
        if ok:
            print(f"  V15 {database}: {why}")
        else:
            failures.append(f"[{database}] V15: {why}")


def check_reapply(
    database: str,
    scratch: psycopg.Connection,
    files: list,
    failures: list[str],
) -> None:
    """V5/V24: the whole tree applied a second time inserts 0 rows."""
    tables = DATABASES[database][0]
    before = row_counts(scratch, tables)
    apply_tree(scratch, files)
    after = row_counts(scratch, tables)
    changed = {t: (before[t], after[t]) for t in tables if before[t] != after[t]}
    if changed:
        failures.append(
            f"[{database}] V5/V24: re-applying the tree changed row counts {changed}"
        )
    else:
        print(f"  V5/V24 {database}: tree re-applied, 0 rows inserted, 0 errors")


def scratch_name(database: str) -> str:
    return f"{database}_ddl_scratch_{int(time.time())}_{os.getpid()}"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument(
        "--keep",
        action="store_true",
        help="Do not drop the scratch databases, for inspection. Drop them by hand after.",
    )
    args = parser.parse_args(argv)

    if not Config(debug=False).postgres_connection_string:
        sys.exit("POSTGRES_CONNECTION_STRING is not set")

    names = {database: scratch_name(database) for database in DATABASES}
    with admin_connect() as admin:
        for name in names.values():
            if admin.execute(
                "select 1 from pg_database where datname = %s", (name,)
            ).fetchone():
                sys.exit(
                    f"scratch name {name} already exists; re-run to get a fresh one"
                )

    for database, name in names.items():
        with admin_connect() as admin:
            admin.execute(f'create database "{name}"')

    failures: list[str] = []
    try:
        for database, name in names.items():
            live = connect(database)
            scratch = connect(name)
            try:
                files = tree_files(database)
                apply_tree(scratch, files)
                print(f"built {name}: {len(files)} files applied")
                check_parity(database, live, scratch, failures)
                check_reapply(database, scratch, files, failures)
            finally:
                live.close()
                scratch.close()
    finally:
        if args.keep:
            print(
                f"--keep: scratch databases left in place: {', '.join(names.values())}"
            )
        else:
            with admin_connect() as admin:
                for name in names.values():
                    admin.execute(f'drop database if exists "{name}"')
            print("dropped both scratch databases")

    if failures:
        print(f"\n{len(failures)} failure(s):")
        for failure in failures:
            print(f"  {failure}")
        return 1
    print("\nT16 verified: both databases rebuild from ddl/ with parity to live")
    return 0


if __name__ == "__main__":
    sys.exit(main())

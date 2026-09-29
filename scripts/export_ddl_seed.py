"""Export the personal merchant seed from a live database into `ddl/seed/`.

`ddl/` is tracked and `ddl/seed/` is gitignored, because a tracked `.sql` file
must never carry a real bank descriptor: the repository is public, and these
rows name insurers, dentists, gyms and specific shops.

This script is the only way those rows get written. It reads a live database
and emits INSERTs at explicit ids, so a rebuilt database carries the same rows
in the same order, and so the seed can always be regenerated from live rather
than being a file somebody has to keep by hand.

It never writes to the database. `--check` is the read-only mode: it renders
the files in memory and reports whether they differ from what is on disk.

Usage:
    uv run python scripts/export_ddl_seed.py
    uv run python scripts/export_ddl_seed.py --check
    uv run python scripts/export_ddl_seed.py --database parents_finance
"""

from __future__ import annotations

import argparse
from pathlib import Path

from db.base import PostgresDB
from utils.repo_paths import repo_root

SEED_DIRNAME = "ddl/seed"

# (table, filename stem, columns). Every id is explicit so the rows survive a
# rebuild in the same order as live.
SeedSpec = tuple[str, str, tuple[str, ...]]

SEED_SPECS: dict[str, tuple[SeedSpec, ...]] = {
    "finance": (
        (
            "substring_auto_match",
            "finance.substring_auto_match",
            ("id", "substring", "merchant_category", "merchant_subcategory"),
        ),
        (
            "merchant_name_auto_match",
            "finance.merchant_name_auto_match",
            ("id", "merchant_name", "merchant_category", "merchant_subcategory"),
        ),
    ),
    "parents_finance": (
        (
            "substring_auto_match",
            "parents_finance.substring_auto_match",
            ("id", "substring", "merchant_category"),
        ),
        (
            "auto_match",
            "parents_finance.auto_match",
            ("id", "merchant_name", "merchant_category"),
        ),
    ),
}

HEADER = """\
-- GENERATED FILE, DO NOT EDIT AND DO NOT COMMIT.
--
-- Written by scripts/export_ddl_seed.py from a live {database}.
-- `ddl/seed/` is gitignored: these rows are real bank descriptors and this
-- repository is public. Regenerate with:
--
--     uv run python scripts/export_ddl_seed.py --database {database}
--
-- Verified against live by `--check` and by tests/test_ddl_seed.py.
"""


def sql_literal(value: object) -> str:
    """One SQL literal: bare for numbers, quoted with doubled quotes for text."""

    if value is None:
        return "null"
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return str(value)
    return "'" + str(value).replace("'", "''") + "'"


def sql_identifier(name: str) -> str:
    """A column name. `substring` is a reserved word, so quote every one."""

    return '"' + name.replace('"', '""') + '"'


def select_rows(db: PostgresDB, table: str, columns: tuple[str, ...]) -> list[tuple]:
    """Rows ordered by id, so the rendered file is stable."""

    joined = ", ".join(columns)
    return db.select(f"select {joined} from {table} order by id")


def render(
    database: str, table: str, stem: str, columns: tuple[str, ...], rows: list[tuple]
) -> str:
    """A runnable seed file: one explicit-id INSERT per row, then a guard."""

    lines = [HEADER.format(database=database), ""]
    lines.append(
        "-- Every row carries its live id. ON CONFLICT DO NOTHING keeps a\n"
        "-- re-apply a no-op instead of an error."
    )
    lines.append("")

    values = ", ".join(sql_identifier(value) for value in columns)
    for row in rows:
        rendered = ", ".join(sql_literal(value) for value in row)
        lines.append(
            f"insert into {sql_identifier(table)} ({values}) values ({rendered}) "
            "on conflict do nothing;"
        )

    lines.append("")
    lines.append(
        "-- The INSERTs above pin explicit ids, so the sequence is still at 1."
    )
    lines.append(
        "-- Every runtime insert in this repo omits the id and relies on nextval,"
    )
    lines.append(
        "-- so without this the first insert after a rebuild collides with a seeded"
    )
    lines.append("-- primary key.")
    lines.append(
        "select setval(pg_get_serial_sequence("
        f"'{table}', 'id'), (select max(id) from {sql_identifier(table)}));"
    )
    lines.append("")
    return "\n".join(lines)


def export(database: str, seed_dir: Path, *, write: bool) -> dict[str, str]:
    """Render every seed file for `database`.

    Reads live and never writes to the database. Writes the files to `seed_dir`
    only when `write` is true, so `--check` cannot touch disk.
    """

    db = PostgresDB(database, debug=False)
    rendered: dict[str, str] = {}

    specs = SEED_SPECS.get(database, ())
    if not specs:
        raise SystemExit(
            f"no seed tables are declared for --database {database}; "
            "add one to SEED_SPECS first"
        )

    for table, stem, columns in specs:
        rows = select_rows(db, table, columns)
        rendered[stem] = render(database, table, stem, columns, rows)

    if write:
        seed_dir.mkdir(parents=True, exist_ok=True)
        for stem, text in rendered.items():
            (seed_dir / f"{stem}.sql").write_text(text, encoding="utf-8")

    return rendered


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=__doc__.split("\n\n")[0],
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--database",
        default="finance",
        choices=sorted(SEED_SPECS),
        help="Which live database to read. Default: finance",
    )
    parser.add_argument(
        "--seed-dir",
        default=None,
        help=f"Where to write. Default: <repo>/{SEED_DIRNAME}",
    )
    parser.add_argument(
        "--check",
        action="store_true",
        help="Do not write. Report whether the files on disk match live.",
    )
    args = parser.parse_args(argv)

    seed_dir = Path(args.seed_dir) if args.seed_dir else repo_root() / SEED_DIRNAME
    rendered = export(args.database, seed_dir, write=not args.check)

    drift = 0
    for stem, text in sorted(rendered.items()):
        path = seed_dir / f"{stem}.sql"
        rows = text.count("insert into")
        if args.check:
            if not path.exists():
                print(f"  MISSING  {path.name}  ({rows} row(s) in live)")
                drift += 1
            elif path.read_text(encoding="utf-8") != text:
                print(f"  DIFFERS  {path.name}  ({rows} row(s) in live)")
                drift += 1
            else:
                print(f"  ok       {path.name}  ({rows} row(s))")
        else:
            print(f"  wrote    {path}  ({rows} row(s))")

    if args.check and drift:
        print(f"\n{drift} seed file(s) differ from live; re-run without --check")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

"""Back up both `finance` auto_match tables before the T9 constraint change.

`pg_dump` is not installed on this machine, so the backup is built here. T9
replaces the 3-column UNIQUE on `merchant_name_auto_match` with a 1-column
UNIQUE, and T8 later asserts `substring_auto_match` still carries its 3-column
UNIQUE. Restoring either table therefore means restoring its exact UNIQUE, not
just its rows, so the `pg_get_constraintdef` text is captured verbatim.

The script refuses to write a backup that does not match the counts and
constraint definitions recorded here. A silent drift between the expected and
actual state means the backup is not the state T9 was planned against, so the
backup is worse than none.

Writes outside the repository, outside git, and outside /tmp.

Usage:
    uv run --frozen python scripts/backup_finance_auto_match.py
"""

from __future__ import annotations

import hashlib
import pathlib
import sys
from datetime import datetime, timezone

import psycopg

from config import Config

DATABASE = "finance"
BACKUP_ROOT = pathlib.Path.home() / "data" / "personal" / "mypersonalfinance-backups"

# (table, key column, expected row count, {constraint name: expected def})
TABLES = (
    (
        "merchant_name_auto_match",
        "merchant_name",
        164,
        {
            "merchant_name_auto_match_pkey": "PRIMARY KEY (id)",
            "merchant_name_auto_match_merchant_name_merchant_category_key": (
                "UNIQUE (merchant_name, merchant_category, merchant_subcategory)"
            ),
        },
    ),
    (
        "substring_auto_match",
        "substring",
        100,
        {
            "substring_auto_match_pkey": "PRIMARY KEY (id)",
            "substring_auto_match_substring_merchant_category_key": (
                'UNIQUE ("substring", merchant_category, merchant_subcategory)'
            ),
        },
    ),
)


def literal(value: object) -> str:
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return str(value)
    return "'" + str(value).replace("'", "''") + "'"


def quote_ident(name: str) -> str:
    return '"' + name.replace('"', '""') + '"'


def read_table(
    cur: psycopg.Cursor, table: str
) -> tuple[list[str], list[tuple], list[tuple], list[tuple]]:
    cur.execute(
        "select a.attname from pg_attribute a where a.attrelid = %s::regclass "
        "and a.attnum > 0 and not a.attisdropped order by a.attnum",
        (table,),
    )
    columns = [r[0] for r in cur.fetchall()]
    cur.execute(f"select {', '.join(columns)} from {table} order by id")
    rows = cur.fetchall()
    cur.execute(
        "select conname, pg_get_constraintdef(oid) from pg_constraint "
        "where conrelid = %s::regclass and contype in ('u','p','f') order by conname",
        (table,),
    )
    constraints = cur.fetchall()
    cur.execute(
        "select a.attname, format_type(a.atttypid, a.atttypmod), a.attnotnull "
        "from pg_attribute a where a.attrelid = %s::regclass and a.attnum > 0 "
        "and not a.attisdropped order by a.attnum",
        (table,),
    )
    column_defs = cur.fetchall()
    return columns, rows, constraints, column_defs


def check_expected(
    table: str, key: str, rows: list, constraints: list, expected
) -> None:
    count, expected_constraints = expected
    if len(rows) != count:
        raise SystemExit(
            f"refusing to back up {table}: expected {count} rows, found {len(rows)}. "
            "The live table drifted from what T9 was planned against."
        )
    actual = dict(constraints)
    if actual != expected_constraints:
        for name, want in expected_constraints.items():
            got = actual.get(name)
            if got != want:
                raise SystemExit(
                    f"refusing to back up {table}: constraint {name} is {got!r}, "
                    f"expected {want!r}"
                )
        extra = sorted(set(actual) - set(expected_constraints))
        raise SystemExit(f"refusing to back up {table}: unexpected constraints {extra}")
    values = [r[1] for r in rows]
    if len(set(values)) != len(values):
        raise SystemExit(
            f"refusing to back up {table}: {key} repeats, so the 1-column UNIQUE "
            "T9 installs would not be accepted"
        )


def main() -> int:
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    out = BACKUP_ROOT / f"finance-auto-match-{stamp}"
    out.mkdir(parents=True, exist_ok=True)

    conn = psycopg.connect(
        f"{Config(debug=False).postgres_connection_string}/{DATABASE}"
    )
    captured: dict[str, dict] = {}
    with conn, conn.cursor() as cur:
        for table, key, count, expected_constraints in TABLES:
            columns, rows, constraints, column_defs = read_table(cur, table)
            check_expected(table, key, rows, constraints, (count, expected_constraints))
            captured[table] = {
                "key": key,
                "columns": columns,
                "rows": rows,
                "constraints": constraints,
                "column_defs": column_defs,
            }

    summary: list[str] = []
    for table, spec in captured.items():
        columns, rows, constraints, column_defs = (
            spec["columns"],
            spec["rows"],
            spec["constraints"],
            spec["column_defs"],
        )
        values = ", ".join(quote_ident(c) for c in columns)
        data = [f"-- finance.{table}: {len(rows)} rows, before the T9 UNIQUE change."]
        for row in rows:
            rendered = ", ".join(literal(v) for v in row)
            data.append(
                f"insert into {quote_ident(table)} ({values}) values ({rendered}) "
                "on conflict do nothing;"
            )
        (out / f"{table}.data.sql").write_text("\n".join(data) + "\n", encoding="utf-8")

        (out / f"{table}.constraints.sql").write_text(
            "\n".join(f"-- {name}: {d}" for name, d in constraints) + "\n",
            encoding="utf-8",
        )
        (out / f"{table}.columns.sql").write_text(
            "\n".join(f"{n} {t} notnull={bool(nn)}" for n, t, nn in column_defs) + "\n",
            encoding="utf-8",
        )

        column_lines = [
            f"    {quote_ident(name)} {typ} {'NOT NULL' if notnull else ''}".rstrip()
            for name, typ, notnull in column_defs
        ]
        constraint_lines = [f"    CONSTRAINT {name} {d}" for name, d in constraints]
        # Every entry but the last needs a trailing comma: the columns and the
        # constraints share one comma-separated list, and a trailing comma
        # before the closing paren is a syntax error.
        body = [
            line + ("," if i < len(column_lines) + len(constraint_lines) - 1 else "")
            for i, line in enumerate(column_lines + constraint_lines)
        ]
        (out / f"{table}.restore.sql").write_text(
            "\n".join(
                [
                    f"-- Rollback for finance.{table}: recreates the table with its live",
                    "-- constraints, then restores every row at its original id.",
                    "",
                    f"DROP TABLE IF EXISTS {quote_ident(table)};",
                    "",
                    f"CREATE TABLE {quote_ident(table)} (",
                    *body,
                    ");",
                    "",
                    *(out / f"{table}.data.sql")
                    .read_text(encoding="utf-8")
                    .splitlines(),
                    "",
                    "SELECT setval(pg_get_serial_sequence("
                    f"'{table}', 'id'), (SELECT max(id) FROM {quote_ident(table)}));",
                    "",
                ]
            ),
            encoding="utf-8",
        )

        digest = hashlib.sha256((out / f"{table}.data.sql").read_bytes()).hexdigest()
        summary.append(f"{table}  rows={len(rows)}  data.sha256={digest}")
        for name, definition in constraints:
            summary.append(f"    {name}: {definition}")

    (out / "MANIFEST.txt").write_text(
        "\n".join(
            [
                f"database   {DATABASE}",
                f"taken      {datetime.now(timezone.utc).isoformat()}",
                "",
                *summary,
                "",
                "T9 drops the 3-column UNIQUE on merchant_name_auto_match and",
                "installs UNIQUE (merchant_name). To undo, run that table's",
                "restore.sql, which recreates the 3-column form with every row",
                "at its original id. substring_auto_match is untouched by T9 and",
                "is backed up so T8 can be re-checked against the original text.",
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    print(f"backup  {out}")
    for line in summary:
        print(f"  {line}")
    for path in sorted(out.iterdir()):
        print(f"  {path.stat().st_size:>7} B  {path.name}")
    return 0


if __name__ == "__main__":
    sys.exit(main())

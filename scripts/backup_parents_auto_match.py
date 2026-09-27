"""Back up `parents_finance.auto_match` before the T24 repair.

`pg_dump` is not installed on this machine, so the backup is built here. It
carries everything a rollback needs: every row, the `CREATE TABLE`, and the
exact `pg_get_constraintdef` text, because restoring the table means restoring
its 2-column UNIQUE exactly as it was.

Writes outside the repository, outside git, and outside /tmp.

Usage:
    uv run --frozen python scripts/backup_parents_auto_match.py
"""

from __future__ import annotations

import hashlib
import pathlib
import sys
from datetime import datetime, timezone

import psycopg

from config import Config

DATABASE = "parents_finance"
TABLE = "auto_match"
COLUMNS = ("id", "merchant_name", "merchant_category")
BACKUP_ROOT = pathlib.Path.home() / "data" / "personal" / "mypersonalfinance-backups"


def literal(value: object) -> str:
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return str(value)
    return "'" + str(value).replace("'", "''") + "'"


def main() -> int:
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    out = BACKUP_ROOT / f"parents-auto-match-repair-{stamp}"
    out.mkdir(parents=True, exist_ok=True)

    conn = psycopg.connect(
        f"{Config(debug=False).postgres_connection_string}/{DATABASE}"
    )
    with conn, conn.cursor() as cur:
        cur.execute(f"select {', '.join(COLUMNS)} from {TABLE} order by id")
        rows = cur.fetchall()
        cur.execute(
            "select conname, pg_get_constraintdef(oid) from pg_constraint "
            "where conrelid = %s::regclass and contype in ('u','p','f') "
            "order by conname",
            (TABLE,),
        )
        constraints = cur.fetchall()
        cur.execute(
            "select a.attname, format_type(a.atttypid, a.atttypmod), a.attnotnull "
            "from pg_attribute a where a.attrelid = %s::regclass and a.attnum > 0 "
            "and not a.attisdropped order by a.attnum",
            (TABLE,),
        )
        columns = cur.fetchall()

    values = ", ".join(f'"{c}"' for c in COLUMNS)
    data = [f"-- parents_finance.{TABLE}: {len(rows)} rows, before the T24 repair."]
    for row in rows:
        rendered = ", ".join(literal(v) for v in row)
        data.append(
            f'insert into "{TABLE}" ({values}) values ({rendered}) on conflict do nothing;'
        )
    (out / "data.sql").write_text("\n".join(data) + "\n", encoding="utf-8")

    (out / "constraints.sql").write_text(
        "\n".join(f"-- {name}: {d}" for name, d in constraints) + "\n",
        encoding="utf-8",
    )
    (out / "columns.sql").write_text(
        "\n".join(f"{n} {t} notnull={bool(nn)}" for n, t, nn in columns) + "\n",
        encoding="utf-8",
    )
    (out / "restore.sql").write_text(
        "\n".join(
            [
                f"-- Rollback for the T24 repair of parents_finance.{TABLE}.",
                "-- Recreates the table with its live constraints, then restores",
                "-- every row at its original id.",
                "",
                f'DROP TABLE IF EXISTS "{TABLE}";',
                "",
                f'CREATE TABLE "{TABLE}" (',
                "    id serial NOT NULL,",
                "    merchant_name text NOT NULL,",
                "    merchant_category text NOT NULL,",
            ]
            + [
                f"    CONSTRAINT {name} {d.replace('UNIQUE ', '')}"
                + ("," if i < len(constraints) - 1 else "")
                for i, (name, d) in enumerate(constraints)
            ]
            + [
                ");",
                "",
                *(out / "data.sql").read_text(encoding="utf-8").splitlines(),
                "",
                "SELECT setval(pg_get_serial_sequence("
                f"'{TABLE}', 'id'), (SELECT max(id) FROM {TABLE}));",
                "",
            ]
        ),
        encoding="utf-8",
    )

    digest = hashlib.sha256((out / "data.sql").read_bytes()).hexdigest()
    (out / "MANIFEST.txt").write_text(
        "\n".join(
            [
                f"database   {DATABASE}",
                f"table      {TABLE}",
                f"rows       {len(rows)}",
                f"data.sql   sha256 {digest}",
                f"taken      {datetime.now(timezone.utc).isoformat()}",
                "",
                "The table is a data repair, not a schema change: restore.sql",
                "drops and recreates it, so run it only to undo the repair.",
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    print(f"backup  {out}")
    print(f"rows    {len(rows)}")
    print(f"sha256  {digest}")
    for name in (
        "data.sql",
        "constraints.sql",
        "columns.sql",
        "restore.sql",
        "MANIFEST.txt",
    ):
        print(f"  {(out / name).stat().st_size:>7} B  {name}")
    return 0


if __name__ == "__main__":
    sys.exit(main())

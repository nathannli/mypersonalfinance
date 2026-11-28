"""Disposable local Postgres holding a copy of prod `finance` and `parents_finance`.

`up` starts a `postgres:18` container (prod runs 18), streams `pg_dump` of both
prod databases straight into `pg_restore` inside it (no dump file on disk),
then checks per-table row counts against prod. `down` removes the container
but leaves its Docker volume, and so the copy, on disk. `docker-clean` removes
this script's container together with that volume; it touches no other
container or volume. Prod is only read: `pg_dump` and `SELECT count(*)`.

Point the app at the copy for one shell with the export line `up` prints:
`load_dotenv` never overrides an exported variable, so `.env` stays as is.

Usage:
    uv run --frozen python scripts/local_test_db.py up
    uv run --frozen python scripts/local_test_db.py down
    uv run --frozen python scripts/local_test_db.py docker-clean
"""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
import time

import psycopg
from dotenv import dotenv_values
from psycopg.conninfo import conninfo_to_dict, make_conninfo

from utils.repo_paths import repo_root

CONTAINER = "mpf-test-pg"
IMAGE = "postgres:18"
LOCAL_PORT = 5433
LOCAL_URI = f"postgresql://postgres:postgres@127.0.0.1:{LOCAL_PORT}"
DATABASES = ("finance", "parents_finance")
LOCAL_HOSTS = {"", "localhost", "127.0.0.1", "::1"}


def require_remote_source(uri: str | None) -> str:
    """The prod URI, refusing anything that could be the local copy itself."""
    if not uri:
        raise SystemExit("POSTGRES_CONNECTION_STRING is missing from .env")
    if conninfo_to_dict(uri).get("host", "") in LOCAL_HOSTS:
        raise SystemExit(
            "refusing: .env POSTGRES_CONNECTION_STRING points at a local host"
        )
    return uri


def prod_uri() -> str:
    # Read .env directly, not Config(): an exported local override would win there.
    return require_remote_source(
        dotenv_values(repo_root() / ".env").get("POSTGRES_CONNECTION_STRING")
    )


def split_password(uri: str, database: str) -> tuple[str, str | None]:
    """A conninfo for `database` without the password, and the password apart."""
    params = conninfo_to_dict(uri)
    password = params.pop("password", None)
    return make_conninfo(**{**params, "dbname": database}), password


def run(*args: str, **kwargs) -> subprocess.CompletedProcess:
    return subprocess.run(args, check=True, **kwargs)


def ensure_docker() -> None:
    if subprocess.run(["colima", "status"], capture_output=True).returncode != 0:
        print("Starting colima...")
        run("colima", "start")


def container_exists() -> bool:
    out = run(
        "docker",
        "ps",
        "-a",
        "--filter",
        f"name=^{CONTAINER}$",
        "--format",
        "{{.Names}}",
        capture_output=True,
        text=True,
    ).stdout
    return out.strip() == CONTAINER


def wait_ready(timeout: float = 60) -> None:
    # TCP, not the socket: the image's init server listens on the socket only.
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        ready = subprocess.run(
            [
                "docker",
                "exec",
                CONTAINER,
                "pg_isready",
                "-h",
                "127.0.0.1",
                "-U",
                "postgres",
            ],
            capture_output=True,
        )
        if ready.returncode == 0:
            return
        time.sleep(1)
    raise SystemExit(f"{CONTAINER} not ready after {timeout:.0f}s")


def table_counts(uri: str) -> dict[str, int]:
    with psycopg.connect(uri) as conn:
        tables = [
            row[0]
            for row in conn.execute(
                "select table_name from information_schema.tables "
                "where table_schema = 'public' and table_type = 'BASE TABLE' "
                "order by table_name"
            )
        ]
        return {
            table: conn.execute(f'select count(*) from public."{table}"').fetchone()[0]
            for table in tables
        }


def up() -> int:
    source = prod_uri()
    ensure_docker()
    if container_exists():
        raise SystemExit(f"{CONTAINER} already exists; run down first")
    run(
        "docker",
        "run",
        "-d",
        "--name",
        CONTAINER,
        "-e",
        "POSTGRES_PASSWORD=postgres",
        "-p",
        f"127.0.0.1:{LOCAL_PORT}:5432",
        IMAGE,
        stdout=subprocess.DEVNULL,
    )
    wait_ready()
    with open(repo_root() / "ddl" / "common" / "databases.sql", "rb") as ddl:
        run(
            "docker",
            "exec",
            "-i",
            CONTAINER,
            "psql",
            "-q",
            "-U",
            "postgres",
            "-v",
            "ON_ERROR_STOP=1",
            stdin=ddl,
        )

    for database in DATABASES:
        print(f"Copying {database}...")
        conninfo, password = split_password(source, database)
        # The password travels as PGPASSWORD, never in any argv or output.
        env = {**os.environ, "SOURCE_DB": conninfo}
        if password is not None:
            env["PGPASSWORD"] = password
        run(
            "docker",
            "exec",
            "-e",
            "SOURCE_DB",
            "-e",
            "PGPASSWORD",
            "-e",
            f"DB={database}",
            CONTAINER,
            "bash",
            "-c",
            'set -o pipefail; pg_dump --format=custom --no-owner --no-privileges -d "$SOURCE_DB"'
            ' | pg_restore -U postgres -d "$DB" --no-owner --no-privileges --exit-on-error',
            env=env,
        )

    failed = False
    for database in DATABASES:
        expected = table_counts(f"{source}/{database}")
        actual = table_counts(f"{LOCAL_URI}/{database}")
        if expected != actual:
            failed = True
            print(f"MISMATCH {database}: prod {expected} local {actual}")
        else:
            print(
                f"{database}: {len(actual)} tables, {sum(actual.values())} rows match prod"
            )
    if failed:
        return 1

    print("\nUse the copy in this shell with:")
    print(f"  export POSTGRES_CONNECTION_STRING={LOCAL_URI}")
    print(
        "Remove it, data included, with: "
        "uv run --frozen python scripts/local_test_db.py docker-clean"
    )
    return 0


def down(remove_volume: bool = False) -> int:
    ensure_docker()
    if not container_exists():
        print(f"{CONTAINER} does not exist")
        return 0
    flags = ["-f", "-v"] if remove_volume else ["-f"]
    run("docker", "rm", *flags, CONTAINER, stdout=subprocess.DEVNULL)
    print(f"Removed {CONTAINER}")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("command", choices=["up", "down", "docker-clean"])
    args = parser.parse_args(argv)
    if args.command == "up":
        return up()
    return down(remove_volume=args.command == "docker-clean")


if __name__ == "__main__":
    sys.exit(main())

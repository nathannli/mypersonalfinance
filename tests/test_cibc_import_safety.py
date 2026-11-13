"""Import downloader in a fresh process with database connections forbidden."""

import subprocess
import sys
import unittest


class TestCibcImportSafety(unittest.TestCase):
    def test_import_does_not_connect_to_postgres(self):
        result = subprocess.run(
            [
                sys.executable,
                "-c",
                """
import psycopg
import sqlalchemy

def forbidden(*args, **kwargs):
    raise AssertionError("Database connection attempted")
psycopg.connect = forbidden
sqlalchemy.create_engine = forbidden
import scripts.download_cibc_transactions
import scripts.cibc_browseros_bridge
""",
            ],
            capture_output=True,
            text=True,
        )
        assert result.returncode == 0, result.stderr

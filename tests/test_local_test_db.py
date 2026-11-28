import unittest

from psycopg.conninfo import conninfo_to_dict

from scripts.local_test_db import require_remote_source, split_password


class RequireRemoteSourceTest(unittest.TestCase):
    def test_refuses_missing_or_local_sources(self):
        for uri in (
            None,
            "",
            "postgresql://postgres:postgres@127.0.0.1:5433",
            "postgresql://u:p@localhost:5432",
            "postgresql://u:p@[::1]:5432",
            "postgresql:///finance",
        ):
            with self.subTest(uri=uri), self.assertRaises(SystemExit):
                require_remote_source(uri)

    def test_accepts_remote_host(self):
        uri = "postgresql://u:p@db.invalid:5432"
        self.assertEqual(require_remote_source(uri), uri)


class SplitPasswordTest(unittest.TestCase):
    def test_password_leaves_the_conninfo_and_options_survive(self):
        conninfo, password = split_password(
            "postgresql://u:s3cret%40pw@db.invalid:5432?sslmode=require", "finance"
        )

        self.assertEqual(password, "s3cret@pw")
        self.assertNotIn("s3cret", conninfo)
        self.assertEqual(
            conninfo_to_dict(conninfo),
            {
                "user": "u",
                "host": "db.invalid",
                "port": "5432",
                "sslmode": "require",
                "dbname": "finance",
            },
        )

    def test_source_without_password(self):
        conninfo, password = split_password(
            "postgresql://u@db.invalid:5432", "parents_finance"
        )

        self.assertIsNone(password)
        self.assertEqual(conninfo_to_dict(conninfo)["dbname"], "parents_finance")


if __name__ == "__main__":
    unittest.main()

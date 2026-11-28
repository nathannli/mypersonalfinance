import unittest
from unittest.mock import patch

from psycopg.conninfo import conninfo_to_dict

from scripts import local_test_db
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


class RemoveContainerTest(unittest.TestCase):
    def removal_args(self, command, exists=True):
        with patch.object(local_test_db, "ensure_docker"):
            with patch.object(local_test_db, "container_exists", return_value=exists):
                with patch.object(local_test_db, "run") as run:
                    self.assertEqual(local_test_db.main([command]), 0)
        return [c.args for c in run.call_args_list]

    def test_down_keeps_the_volume(self):
        self.assertEqual(
            self.removal_args("down"), [("docker", "rm", "-f", "mpf-test-pg")]
        )

    def test_docker_clean_removes_this_container_and_its_volume(self):
        self.assertEqual(
            self.removal_args("docker-clean"),
            [("docker", "rm", "-f", "-v", "mpf-test-pg")],
        )

    def test_docker_clean_without_a_container_removes_nothing(self):
        self.assertEqual(self.removal_args("docker-clean", exists=False), [])


if __name__ == "__main__":
    unittest.main()

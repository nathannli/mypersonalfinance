import unittest

from scripts.local_test_db import require_remote_source


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


if __name__ == "__main__":
    unittest.main()

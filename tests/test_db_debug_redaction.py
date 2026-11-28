import io
import os
import unittest
from contextlib import redirect_stdout
from unittest.mock import patch

from db.parents_finance import ParentsFinanceDB


class DebugOutputRedactionTest(unittest.TestCase):
    def test_debug_init_does_not_print_the_connection_string(self):
        url = "postgresql://someuser:s3cret-pw@db.invalid:5432"
        output = io.StringIO()
        with patch.dict(os.environ, {"POSTGRES_CONNECTION_STRING": url}):
            with redirect_stdout(output):
                database = ParentsFinanceDB(debug=True)

        self.assertEqual(database.uri, f"{url}/parents_finance")
        for secret in ("s3cret-pw", "someuser", "db.invalid"):
            self.assertNotIn(secret, output.getvalue())
        self.assertIn("parents_finance", output.getvalue())


if __name__ == "__main__":
    unittest.main()

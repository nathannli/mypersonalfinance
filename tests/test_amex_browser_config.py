import unittest
from pathlib import Path

from config import AMEX_BROWSER_PROFILE_DIR


class TestAmexBrowserConfig(unittest.TestCase):
    def test_profile_directory_is_outside_repository(self) -> None:
        repository = Path(__file__).resolve().parents[1]

        self.assertFalse(AMEX_BROWSER_PROFILE_DIR.is_relative_to(repository))
        self.assertEqual(AMEX_BROWSER_PROFILE_DIR.name, "amex-browser-profile")


if __name__ == "__main__":
    unittest.main()

import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, Mock

from scripts.download_amex_transactions import (
    capture_download,
    destination_paths,
    parse_args,
    sanitize_filename,
)


class TestDownloadAmexTransactions(unittest.TestCase):
    def test_required_cli_arguments(self) -> None:
        args = parse_args(
            ["--output-dir", "/tmp/amex", "--database", "parents_finance"]
        )

        self.assertEqual(args.output_dir, Path("/tmp/amex"))
        self.assertEqual(args.database, "parents_finance")

    def test_sanitize_filename_removes_path_and_unsafe_characters(self) -> None:
        self.assertEqual(sanitize_filename("../../activity (1).csv"), "activity_1_.csv")

    def test_destination_refuses_overwrite(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            existing = Path(directory) / "activity.csv"
            existing.touch()

            with self.assertRaisesRegex(FileExistsError, "Refusing to overwrite"):
                destination_paths(Path(directory), "activity.csv")

    def test_failed_save_removes_partial_file(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            page = MagicMock()
            download = Mock(suggested_filename="activity.csv")
            page.expect_download.return_value.__enter__.return_value.value = download

            def fail_save(path: Path) -> None:
                Path(path).touch()
                raise OSError("download interrupted")

            download.save_as.side_effect = fail_save

            with self.assertRaisesRegex(RuntimeError, "download interrupted"):
                capture_download(page, Path(directory))

            self.assertEqual(list(Path(directory).iterdir()), [])


if __name__ == "__main__":
    unittest.main()

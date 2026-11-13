import csv
from datetime import date

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from scripts import download_cibc_transactions as cli


class TestDownloadCibcTransactions(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.tmp_path = Path(directory.name)

    def test_filter_and_parser(self):
        with self.assertLogs(cli.logger, level="INFO") as logs:
            tmp_path = self.tmp_path
            raw = tmp_path / "raw" / "cibc.csv"
            raw.parent.mkdir()
            raw.write_text(
                "2026-10-01,SHOP,10,,XXXX\n2026-10-02,PAYMENT,,10,XXXX\n2026-10-03,REFUND,,2,XXXX\n2026-09-30,OLD,3,,XXXX\n"
            )

            async def scrape(*args):
                return [raw]

            self.enterContext(patch.object(cli, "run_scrape", scrape))
            self.enterContext(
                patch.object(cli, "credentials", lambda: ("user", "password"))
            )
            target = cli.run(
                tmp_path / "out",
                "parents_finance",
                date(2026, 10, 1),
                date(2026, 10, 3),
            )
            with target.open() as handle:
                assert len(list(csv.reader(handle))) == 3
            from sources.csv.cibc_mc import CibcMcStatement

            assert CibcMcStatement(str(target)).get_df().height == 1
            assert "--type cibc_mc" in "\n".join(logs.output)
            assert "--database parents_finance" in "\n".join(logs.output)
            assert "2 previous business days" in "\n".join(logs.output)
            assert not raw.exists()

    def test_invalid_export_leaves_no_target(self):
        for text in ["", "bad,SHOP,1,,XXXX\n", "2026-10-01,SHOP,1\n"]:
            with self.subTest(text=text):
                tmp_path = self.tmp_path
                raw = tmp_path / "raw.csv"
                raw.write_text(text)
                target = tmp_path / "target.csv"
                with self.assertRaises(RuntimeError):
                    cli.combine_exports(
                        [raw], date(2026, 10, 1), date(2026, 10, 31), target
                    )
                assert not target.exists()

    def test_collision(self):
        tmp_path = self.tmp_path
        target = tmp_path / "target.csv"
        target.write_text("unchanged")
        raw = tmp_path / "raw.csv"
        raw.write_text("2026-10-01,SHOP,1,,XXXX\n")
        with self.assertRaises(FileExistsError):
            cli.combine_exports([raw], date(2026, 10, 1), date(2026, 10, 31), target)
        assert target.read_text() == "unchanged"

    def test_invalid_money_leaves_no_output(self):
        with self.assertLogs(cli.logger, level="INFO") as logs:
            for amount, error in [
                ("not-money", "invalid five-column"),
                ("1e999", "invalid five-column"),
                ("1_000", "invalid five-column"),
                ("9" * 400 + ".00", "parser validation"),
            ]:
                with self.subTest(amount=amount, error=error):
                    tmp_path = self.tmp_path
                    raw = tmp_path / "raw" / "cibc.csv"
                    raw.parent.mkdir()
                    raw.write_text(f"2026-10-01,SHOP,{amount},,XXXX\n")

                    async def scrape(*args):
                        return [raw]

                    self.enterContext(patch.object(cli, "run_scrape", scrape))
                    self.enterContext(
                        patch.object(cli, "credentials", lambda: ("user", "password"))
                    )
                    with self.assertRaisesRegex(RuntimeError, error):
                        cli.run(
                            tmp_path / "out",
                            "parents_finance",
                            date(2026, 10, 1),
                            date(2026, 10, 31),
                        )
                    assert not list((tmp_path / "out").glob("*.csv"))
                    assert not raw.exists()
                    assert "Load manually:" not in "\n".join(logs.output)

    def test_preflight_before_credentials(self):
        for since, until in [
            (date(2011, 12, 10), date(2026, 1, 1)),
            (date(2026, 2, 1), date(2026, 1, 1)),
        ]:
            with self.subTest(since=since, until=until):
                tmp_path = self.tmp_path
                self.enterContext(
                    patch.object(
                        cli, "credentials", lambda: self.fail("credentials reached")
                    )
                )
                with self.assertRaises(ValueError):
                    cli.run(tmp_path, "parents_finance", since, until)

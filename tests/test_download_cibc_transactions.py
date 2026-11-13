import csv
from datetime import date

import pytest

from scripts import download_cibc_transactions as cli


def test_filter_and_parser(tmp_path, monkeypatch, caplog):
    raw = tmp_path / "raw" / "cibc.csv"
    raw.parent.mkdir()
    raw.write_text(
        "2026-10-01,SHOP,10,,XXXX\n2026-10-02,PAYMENT,,10,XXXX\n2026-10-03,REFUND,,2,XXXX\n2026-09-30,OLD,3,,XXXX\n"
    )

    async def scrape(*args):
        return [raw]

    monkeypatch.setattr(cli, "run_scrape", scrape)
    monkeypatch.setattr(cli, "credentials", lambda: ("user", "password"))
    caplog.set_level("INFO")
    target = cli.run(
        tmp_path / "out", "parents_finance", date(2026, 10, 1), date(2026, 10, 3)
    )
    assert len(list(csv.reader(target.open()))) == 3
    from sources.csv.cibc_mc import CibcMcStatement

    assert CibcMcStatement(str(target)).get_df().height == 1
    assert "--type cibc_mc" in caplog.text
    assert "--database parents_finance" in caplog.text
    assert "2 previous business days" in caplog.text
    assert not raw.exists()


@pytest.mark.parametrize("text", ["", "bad,SHOP,1,,XXXX\n", "2026-10-01,SHOP,1\n"])
def test_invalid_export_leaves_no_target(tmp_path, text):
    raw = tmp_path / "raw.csv"
    raw.write_text(text)
    target = tmp_path / "target.csv"
    with pytest.raises(RuntimeError):
        cli.combine_exports([raw], date(2026, 10, 1), date(2026, 10, 31), target)
    assert not target.exists()


def test_collision(tmp_path):
    target = tmp_path / "target.csv"
    target.write_text("unchanged")
    raw = tmp_path / "raw.csv"
    raw.write_text("2026-10-01,SHOP,1,,XXXX\n")
    with pytest.raises(FileExistsError):
        cli.combine_exports([raw], date(2026, 10, 1), date(2026, 10, 31), target)
    assert target.read_text() == "unchanged"


@pytest.mark.parametrize(
    "amount,error",
    [
        ("not-money", "invalid five-column"),
        ("1e999", "invalid five-column"),
        ("1_000", "invalid five-column"),
        ("9" * 400 + ".00", "parser validation"),
    ],
)
def test_invalid_money_leaves_no_output(tmp_path, monkeypatch, caplog, amount, error):
    caplog.set_level("INFO")
    raw = tmp_path / "raw" / "cibc.csv"
    raw.parent.mkdir()
    raw.write_text(f"2026-10-01,SHOP,{amount},,XXXX\n")

    async def scrape(*args):
        return [raw]

    monkeypatch.setattr(cli, "run_scrape", scrape)
    monkeypatch.setattr(cli, "credentials", lambda: ("user", "password"))
    with pytest.raises(RuntimeError, match=error):
        cli.run(
            tmp_path / "out", "parents_finance", date(2026, 10, 1), date(2026, 10, 31)
        )
    assert not list((tmp_path / "out").glob("*.csv"))
    assert not raw.exists()
    assert "Load manually:" not in caplog.text


@pytest.mark.parametrize(
    "since,until",
    [(date(2011, 12, 10), date(2026, 1, 1)), (date(2026, 2, 1), date(2026, 1, 1))],
)
def test_preflight_before_credentials(tmp_path, monkeypatch, since, until):
    monkeypatch.setattr(cli, "credentials", lambda: pytest.fail("credentials reached"))
    with pytest.raises(ValueError):
        cli.run(tmp_path, "parents_finance", since, until)

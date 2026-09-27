"""``stonks ingest borrow`` wiring (roadmap 19.3). The FTP transport is
replaced by canned files, so nothing touches the network."""

from __future__ import annotations

from datetime import date

import pytest
from typer.testing import CliRunner

from stonks.cli import app
from stonks.ingest.sources import ibkr_borrow
from stonks.store.lake import DuckDBLake

USA = """#BOF|2026.09.28|09:45:03
#SYM|CUR|NAME|CON|ISIN|REBATERATE|FEERATE|AVAILABLE|
AAPL|USD|APPLE INC|265598|US0378331005|4.57|0.25|>10000000|
#EOF|1
"""


@pytest.fixture
def cli_env(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "config").mkdir()
    (tmp_path / "config" / "default.toml").write_text(
        '[lake]\npath = "data/lake.duckdb"\n\n[state]\npath = "data/state.sqlite"\n'
        '\n[sources.ibkr_borrow]\nmarkets = ["usa"]\n'
    )
    asked: list[str] = []

    def fetcher(host, user, timeout):
        assert (host, user) == ("ftp2.interactivebrokers.com", "shortstock")

        def fetch(name: str) -> str:
            asked.append(name)
            if name != "usa.txt":
                raise OSError("no such file")
            return USA

        return fetch

    monkeypatch.setattr(ibkr_borrow, "ftp_fetcher", fetcher)
    return tmp_path, asked


def test_ingest_borrow_writes_the_default_markets(cli_env):
    tmp_path, asked = cli_env
    result = CliRunner().invoke(app, ["ingest", "borrow"])
    assert result.exit_code == 0, result.output
    assert asked == ["usa.txt"]
    lake = DuckDBLake(tmp_path / "data" / "lake.duckdb")
    try:
        row = lake.borrow_rate("AAPL.US", date(2026, 9, 28))
    finally:
        lake.close()
    assert row is not None
    assert row["fee_rate_annual"] == pytest.approx(0.0025)


def test_ingest_borrow_soft_fails_a_missing_market(cli_env):
    _, asked = cli_env
    result = CliRunner().invoke(app, ["ingest", "borrow", "--markets", "usa,uk"])
    assert result.exit_code == 0, result.output
    assert asked == ["usa.txt", "uk.txt"]
    assert "partial" in result.output


def test_ingest_borrow_refuses_an_unknown_market(cli_env):
    result = CliRunner().invoke(app, ["ingest", "borrow", "--markets", "atlantis"])
    assert result.exit_code != 0

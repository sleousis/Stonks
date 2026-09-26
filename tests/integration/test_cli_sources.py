"""``--source`` selection on the ingest commands (hermetic: yfinance is faked)."""

from __future__ import annotations

from datetime import datetime

import duckdb
import pandas as pd
import pytest
from typer.testing import CliRunner

from stonks.cli import app
from stonks.ingest.sources import registry
from stonks.ingest.sources.yahoo import YahooDataSource


class _FakeTicker:
    def __init__(self, symbol, calls):
        self.symbol = symbol
        self._calls = calls

    def history(self, **kwargs):
        self._calls.append((self.symbol, kwargs))
        idx = pd.DatetimeIndex([datetime(2026, 4, 1)]).tz_localize("Europe/Berlin")
        return pd.DataFrame(
            {
                "Open": [10.0],
                "High": [11.0],
                "Low": [9.0],
                "Close": [10.5],
                "Adj Close": [10.2],
                "Volume": [1000],
            },
            index=idx,
        )

    @property
    def info(self):
        return {
            "quoteType": "EQUITY",
            "longName": "Bayerische Motoren Werke AG",
            "sector": "Consumer Cyclical",
            "industry": "Auto Manufacturers",
            "currency": "EUR",
        }


@pytest.fixture
def runner():
    return CliRunner()


@pytest.fixture
def cli_env(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "config").mkdir()
    (tmp_path / "config" / "default.toml").write_text(
        '[lake]\npath = "data/lake.duckdb"\n\n[state]\npath = "data/state.sqlite"\n'
    )
    monkeypatch.delenv("EODHD_API_KEY", raising=False)
    # The CLI loads .env by searching upward from cli.py, which can find a
    # developer's real key; keep these tests hermetic.
    import dotenv

    monkeypatch.setattr(dotenv, "load_dotenv", lambda *a, **k: False)
    return tmp_path


@pytest.fixture
def yahoo_calls(monkeypatch):
    calls: list = []

    def fake_factory(cfg):
        return YahooDataSource(
            ticker_factory=lambda s: _FakeTicker(s, calls),
            min_request_interval_seconds=0.0,
            sleep=lambda s: None,
        )

    monkeypatch.setitem(registry._FACTORIES, "yahoo", fake_factory)
    return calls


def _lake(cli_env):
    return duckdb.connect(str(cli_env / "data" / "lake.duckdb"), read_only=True)


def test_ingest_prices_from_yahoo_lands_in_same_tables(runner, cli_env, yahoo_calls):
    result = runner.invoke(app, ["ingest", "prices", "--source", "yahoo", "--tickers", "BMW.XETRA"])
    assert result.exit_code == 0, result.output
    assert "status=ok" in result.output
    assert yahoo_calls[0][0] == "BMW.DE"

    con = _lake(cli_env)
    rows = con.execute(
        "SELECT ticker, CAST(timestamp AS DATE), close, adj_close FROM bars WHERE interval = '1d'"
    ).fetchall()
    assert [(r[0], str(r[1]), r[2], r[3]) for r in rows] == [
        ("BMW.XETRA", "2026-04-01", 10.5, 10.2)
    ]
    assert con.execute("SELECT source FROM ingest_runs").fetchone()[0] == "yahoo"


def test_ingest_metadata_from_yahoo_populates_instruments(runner, cli_env, yahoo_calls):
    result = runner.invoke(
        app, ["ingest", "metadata", "--source", "yahoo", "--tickers", "BMW.XETRA"]
    )
    assert result.exit_code == 0, result.output
    row = (
        _lake(cli_env)
        .execute(
            "SELECT exchange, currency, sector, industry FROM instruments WHERE id = ?",
            ["BMW.XETRA"],
        )
        .fetchone()
    )
    assert row == ("XETRA", "EUR", "Consumer Cyclical", "Auto Manufacturers")


def test_ingest_fundamentals_from_yahoo_soft_fails(runner, cli_env, yahoo_calls):
    result = runner.invoke(
        app, ["ingest", "fundamentals", "--source", "yahoo", "--tickers", "BMW.XETRA"]
    )
    assert result.exit_code == 0, result.output
    assert "status=error" in result.output


def test_yahoo_exchange_discovery_is_a_usage_error(runner, cli_env, yahoo_calls):
    result = runner.invoke(app, ["ingest", "prices", "--source", "yahoo", "--exchange", "US"])
    assert result.exit_code == 2, result.output
    assert "--tickers" in result.output


def test_unknown_source_is_a_usage_error(runner, cli_env):
    result = runner.invoke(app, ["ingest", "prices", "--source", "nope", "--tickers", "AAPL.US"])
    assert result.exit_code == 2, result.output
    assert "yahoo" in result.output


def test_default_source_is_eodhd(runner, cli_env, monkeypatch):
    from stonks import cli as cli_module

    seen: list[str] = []

    def fake_build(settings, source_id):
        seen.append(source_id)
        raise cli_module.typer.Exit(code=0)

    monkeypatch.setattr(cli_module, "_build_source", fake_build)
    result = runner.invoke(app, ["ingest", "prices", "--tickers", "AAPL.US"])
    assert result.exit_code == 0, result.output
    assert seen == ["eodhd"]


def test_missing_eodhd_key_is_a_usage_error(runner, cli_env):
    result = runner.invoke(app, ["ingest", "prices", "--tickers", "AAPL.US"])
    assert result.exit_code == 2, result.output
    assert "EODHD_API_KEY" in result.output


@pytest.mark.parametrize(
    "command",
    [
        ["exchanges"],
        ["prices", "--tickers", "X.US"],
        ["fundamentals", "--tickers", "X.US"],
        ["metadata", "--tickers", "X.US"],
        ["intraday", "--tickers", "X.US"],
        ["macro", "--countries", "USA"],
        ["all-intervals", "--tickers", "X.US"],
    ],
)
def test_every_ingest_command_accepts_source(runner, cli_env, monkeypatch, command):
    from stonks import cli as cli_module

    seen: list[str] = []

    def fake_build(settings, source_id):
        seen.append(source_id)
        raise cli_module.typer.Exit(code=0)

    monkeypatch.setattr(cli_module, "_build_source", fake_build)
    result = runner.invoke(app, ["ingest", *command, "--source", "yahoo"])
    assert result.exit_code == 0, result.output
    assert seen == ["yahoo"]

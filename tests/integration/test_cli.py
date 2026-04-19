"""Smoke tests for the Typer CLI. Uses a fake source injected via a monkeypatch
so we exercise the full wiring without touching the network."""

from __future__ import annotations

from datetime import date

import pytest
from typer.testing import CliRunner

from stonks.cli import app
from stonks.ingest.schemas import RawPriceBar
from stonks.ingest.sources.base import DataSource


class _FakeSource(DataSource):
    source_id = "eodhd"

    def list_tickers(self, exchange):
        return ["AAPL.US"]

    def fetch_prices(self, ticker, since=None, until=None):
        return [
            RawPriceBar(
                ticker=ticker,
                date=date(2026, 4, 1),
                open=100.0,
                high=105.0,
                low=99.0,
                close=104.0,
                adj_close=104.0,
                volume=1_000_000,
            )
        ]

    def fetch_fundamentals(self, ticker):
        return []


@pytest.fixture
def runner():
    return CliRunner()


@pytest.fixture
def cli_env(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "config").mkdir()
    (tmp_path / "config" / "default.toml").write_text(
        """
[lake]
path = "data/lake.duckdb"

[state]
path = "data/state.sqlite"

[sources.eodhd]
base_url = "https://example.test/api"
""".strip()
    )
    monkeypatch.setenv("EODHD_API_KEY", "test-key")
    return tmp_path


def test_db_init_creates_lake_and_state(runner, cli_env):
    result = runner.invoke(app, ["db", "init"])
    assert result.exit_code == 0, result.output
    assert (cli_env / "data" / "lake.duckdb").exists()
    assert (cli_env / "data" / "state.sqlite").exists()


def test_db_info_lists_tables_from_both_stores(runner, cli_env):
    runner.invoke(app, ["db", "init"])
    result = runner.invoke(app, ["db", "info"])
    assert result.exit_code == 0, result.output
    # lake tables
    assert "prices" in result.output
    assert "fundamentals" in result.output
    assert "ingest_runs" in result.output
    # state tables
    assert "strategies" in result.output
    assert "orders" in result.output
    assert "tick_runs" in result.output


def test_ingest_prices_via_fake_source(runner, cli_env, monkeypatch):
    from stonks import cli as cli_module

    monkeypatch.setattr(cli_module, "_build_source", lambda settings: _FakeSource())

    runner.invoke(app, ["db", "init"])
    result = runner.invoke(
        app,
        ["ingest", "prices", "--tickers", "AAPL.US", "--since", "2026-03-01"],
    )
    assert result.exit_code == 0, result.output
    assert "ok" in result.output.lower()


def test_ingest_metadata_via_fake_source(runner, cli_env, monkeypatch):
    """`stonks ingest metadata` runs against a FakeSource that returns a
    minimal non-empty MetadataBundle; the run should report ok."""
    from datetime import date as _date

    from stonks import cli as cli_module
    from stonks.ingest.metadata_bundle import MetadataBundle
    from stonks.ingest.schemas import DividendRow, TickerProfile

    class _FakeMetaSource(_FakeSource):
        def fetch_metadata(self, ticker):
            return MetadataBundle(
                profile=TickerProfile(id=ticker, name="Apple Inc", sector="Technology"),
                dividends=(
                    DividendRow(
                        ticker=ticker, ex_date=_date(2026, 2, 10),
                        amount=0.25, currency="USD",
                    ),
                ),
            )

    monkeypatch.setattr(cli_module, "_build_source", lambda settings: _FakeMetaSource())

    runner.invoke(app, ["db", "init"])
    result = runner.invoke(app, ["ingest", "metadata", "--tickers", "AAPL.US"])
    assert result.exit_code == 0, result.output
    assert "ok" in result.output.lower()
    assert "metadata" in result.output

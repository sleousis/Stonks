"""CLI tests for `stonks audit statements` and the audit after
`stonks ingest fundamentals` (BL-36)."""

from __future__ import annotations

from datetime import date

import pytest
from typer.testing import CliRunner

from stonks.cli import app
from stonks.ingest.schemas import BalanceSheetRow, FinancialStatementsBundle
from stonks.ingest.sources.base import DataSource
from stonks.store.lake import DuckDBLake

CONFIG = """
[lake]
path = "data/lake.duckdb"

[state]
path = "data/state.sqlite"
""".strip()


def _balance(ticker: str, liabilities: float) -> BalanceSheetRow:
    return BalanceSheetRow(
        ticker=ticker,
        period_end=date(2024, 12, 31),
        frequency="A",
        filing_date=date(2025, 2, 1),
        total_assets=1000.0,
        total_liabilities=liabilities,
        total_stockholder_equity=400.0,
    )


class _StatementsSource(DataSource):
    source_id = "eodhd"

    def list_tickers(self, exchange):
        return []

    def fetch_prices(self, ticker, since=None, until=None):
        return []

    def fetch_fundamentals(self, ticker):
        # BAD.US breaks assets = liabilities + equity; GOOD.US balances.
        liabilities = 100.0 if ticker == "BAD.US" else 600.0
        return FinancialStatementsBundle(balance=(_balance(ticker, liabilities),))

    def fetch_metadata(self, ticker):
        raise NotImplementedError


@pytest.fixture
def runner():
    return CliRunner()


@pytest.fixture
def cli_env(tmp_path, monkeypatch, runner):
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("STONKS_DATA_DIR", raising=False)
    (tmp_path / "config").mkdir()
    (tmp_path / "config" / "default.toml").write_text(CONFIG)
    from stonks import cli as cli_module

    source = _StatementsSource()
    monkeypatch.setattr(cli_module, "_build_source", lambda settings, source_id="eodhd": source)
    assert runner.invoke(app, ["db", "init"]).exit_code == 0
    return tmp_path


def _flags(env) -> list[tuple[str, str]]:
    lake = DuckDBLake(env / "data" / "lake.duckdb")
    try:
        df = lake.get_statement_flags()
    finally:
        lake.close()
    return sorted(zip(df["ticker"], df["check_id"], strict=True))


def test_ingest_fundamentals_audits_the_ingested_tickers(runner, cli_env):
    result = runner.invoke(app, ["ingest", "fundamentals", "--tickers", "BAD.US,GOOD.US"])
    assert result.exit_code == 0, result.output
    assert "balance_identity" in result.output
    assert _flags(cli_env) == [("BAD.US", "balance_identity")]


def test_audit_statements_reports_and_writes_flags(runner, cli_env):
    runner.invoke(app, ["ingest", "fundamentals", "--tickers", "BAD.US,GOOD.US"])
    lake = DuckDBLake(cli_env / "data" / "lake.duckdb")
    lake.con.execute("DELETE FROM statement_flags")
    lake.close()

    result = runner.invoke(app, ["audit", "statements", "--tickers", "BAD.US"])
    assert result.exit_code == 0, result.output
    assert "balance_identity" in result.output
    assert _flags(cli_env) == [("BAD.US", "balance_identity")]

    everyone = runner.invoke(app, ["audit", "statements"])
    assert everyone.exit_code == 0, everyone.output
    assert _flags(cli_env) == [("BAD.US", "balance_identity")]


def test_audit_tolerances_come_from_config(runner, cli_env):
    runner.invoke(app, ["ingest", "fundamentals", "--tickers", "BAD.US"])
    (cli_env / "config" / "default.toml").write_text(CONFIG + "\n\n[audit]\nbalance = 0.9\n")
    result = runner.invoke(app, ["audit", "statements"])
    assert result.exit_code == 0, result.output
    assert _flags(cli_env) == []
    assert "no flags" in result.output

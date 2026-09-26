"""``stonks ingest tvl`` wiring. The source is a stub injected via
monkeypatch, so nothing touches the network."""

from __future__ import annotations

from datetime import date

import pytest
from typer.testing import CliRunner

from stonks import cli as cli_module
from stonks.cli import app
from stonks.ingest.schemas import DefiTvlRow, FinancialStatementsBundle
from stonks.ingest.sources.base import DataSource
from stonks.store.lake import DuckDBLake


class _StubTvlSource(DataSource):
    source_id = "defillama"

    def __init__(self):
        self.calls: list[tuple[str, date | None]] = []
        self.built_with: list[str] = []

    def list_tickers(self, exchange):
        return []

    def fetch_prices(self, ticker, since=None, until=None):
        return []

    def fetch_fundamentals(self, ticker):
        return FinancialStatementsBundle()

    def fetch_chain_tvl(self, chain, since=None):
        self.calls.append((chain, since))
        return [
            DefiTvlRow(
                chain=chain,
                observation_date=date(2026, 1, 2),
                tvl_usd=1e9,
                source=self.source_id,
            )
        ]


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
    return tmp_path


@pytest.fixture
def stub(monkeypatch):
    source = _StubTvlSource()

    def _build(settings, source_id="eodhd"):
        source.built_with.append(source_id)
        return source

    monkeypatch.setattr(cli_module, "_build_source", _build)
    return source


def test_ingest_tvl_writes_rows_and_ingest_run(runner, cli_env, stub):
    result = runner.invoke(
        app, ["ingest", "tvl", "--chains", "Ethereum, solana", "--since", "2026-01-01"]
    )
    assert result.exit_code == 0, result.output
    assert "ok" in result.output.lower()
    assert stub.built_with == ["defillama"]  # TVL defaults to the DefiLlama source
    assert stub.calls == [("ethereum", date(2026, 1, 1)), ("solana", date(2026, 1, 1))]
    lake = DuckDBLake(cli_env / "data" / "lake.duckdb")
    try:
        assert len(lake.sql("SELECT * FROM defi_tvl")) == 2
        runs = lake.sql("SELECT source, kind, status, tickers_ok FROM ingest_runs")
    finally:
        lake.close()
    assert runs.iloc[0].tolist() == ["defillama", "defi_tvl", "ok", 2]


def test_ingest_tvl_requires_chains(runner, cli_env, stub):
    result = runner.invoke(app, ["ingest", "tvl", "--chains", " , "])
    assert result.exit_code != 0
    assert stub.calls == []


def test_ingest_tvl_rejects_bad_since(runner, cli_env, stub):
    result = runner.invoke(app, ["ingest", "tvl", "--chains", "ethereum", "--since", "01-01-2026"])
    assert result.exit_code != 0
    assert stub.calls == []

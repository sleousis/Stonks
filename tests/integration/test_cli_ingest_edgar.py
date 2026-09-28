"""``stonks ingest edgar`` (roadmap 23.13): argument checks, the User-Agent
requirement and a run over the recorded fixtures."""

from __future__ import annotations

import pytest
from typer.testing import CliRunner

from stonks.cli import app
from stonks.ingest.sources.edgar import EdgarDataSource
from stonks.store.lake import DuckDBLake
from tests.unit.test_edgar_source import _source

CONFIG = """
[lake]
path = "data/lake.duckdb"

[state]
path = "data/state.sqlite"
""".strip()


@pytest.fixture
def workdir(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("STONKS_DATA_DIR", raising=False)
    monkeypatch.delenv("STONKS_EDGAR_USER_AGENT", raising=False)
    (tmp_path / "config").mkdir()
    (tmp_path / "config" / "default.toml").write_text(CONFIG)
    (tmp_path / "data").mkdir()
    return tmp_path


def test_needs_a_user_agent(workdir):
    out = CliRunner().invoke(app, ["ingest", "edgar", "--tickers", "AAPL.US"])
    assert out.exit_code != 0
    assert "User-Agent" in out.output


def test_argument_checks(workdir):
    runner = CliRunner()
    assert runner.invoke(app, ["ingest", "edgar", "--kinds", "prices"]).exit_code != 0
    assert runner.invoke(app, ["ingest", "edgar"]).exit_code != 0
    assert runner.invoke(app, ["ingest", "edgar", "--kinds", "holdings"]).exit_code != 0
    bad = runner.invoke(app, ["ingest", "edgar", "--kinds", "holdings", "--filers", "BRK"])
    assert bad.exit_code != 0


def test_ingests_filings_insiders_and_holdings(workdir, monkeypatch):
    monkeypatch.setattr(EdgarDataSource, "from_config", classmethod(lambda cls, cfg: _source()))
    out = CliRunner().invoke(
        app,
        [
            "ingest", "edgar", "--tickers", "AAPL.US", "--filers", "1067983",
            "--kinds", "filings,insiders,holdings", "--since", "2026-04-01",
        ],
    )  # fmt: skip
    assert out.exit_code == 0, out.output
    lake = DuckDBLake(workdir / "data" / "lake.duckdb")
    try:
        forms = lake.get_corporate_filings(["AAPL.US"])["form"].tolist()
        assert forms == ["8-K", "8-K", "10-Q"]  # [sources.edgar] forms, from --since
        assert len(lake.get_insider_transactions("AAPL.US")) == 1
        assert len(lake.get_institutional_holdings(filer_cik="0001067983")) == 3
    finally:
        lake.close()

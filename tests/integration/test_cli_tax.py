"""CLI tests for `stonks tax` and `stonks ingest fx` (roadmap 20.5)."""

from __future__ import annotations

import csv
import io
from datetime import date

import pytest
from typer.testing import CliRunner

import stonks.cli as cli
from stonks.cli import app
from stonks.store.lake import DuckDBLake
from stonks.store.state import SqliteState
from tests.unit.test_fx_ingest import _FakeFx

CONFIG = """
[lake]
path = "data/lake.duckdb"

[state]
path = "data/state.sqlite"

[registry]
artifacts_dir = "data/artifacts"
""".strip()


@pytest.fixture
def runner():
    return CliRunner()


@pytest.fixture
def workdir(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("STONKS_DATA_DIR", raising=False)
    monkeypatch.setenv("COLUMNS", "240")
    (tmp_path / "config").mkdir()
    (tmp_path / "config" / "default.toml").write_text(CONFIG)
    (tmp_path / "data").mkdir()
    state = SqliteState(tmp_path / "data" / "state.sqlite")
    state.migrate()
    for cid, side, price, at in [
        ("b1", "buy", 100.0, "2025-01-02T15:00:00+00:00"),
        ("s1", "sell", 130.0, "2025-06-02T15:00:00+00:00"),
    ]:
        state.execute(
            "INSERT INTO orders (client_id, ticker, side, quantity, order_type, status,"
            " created_at, updated_at, portfolio_id) VALUES (?, 'UP.US', ?, 5, 'market',"
            " 'filled', ?, ?, 'pf_default')",
            [cid, side, at, at],
        )
        state.execute(
            "INSERT INTO fills (order_client_id, ticker, quantity, price, fee, filled_at,"
            " portfolio_id) VALUES (?, 'UP.US', 5, ?, 0, ?, 'pf_default')",
            [cid, price, at],
        )
    state.close()
    lake = DuckDBLake(tmp_path / "data" / "lake.duckdb")
    lake.migrate()
    lake.close()
    return tmp_path


def test_tax_gains_to_stdout_and_file(runner, workdir):
    result = runner.invoke(app, ["tax", "gains", "--year", "2025"])
    assert result.exit_code == 0, result.output
    rows = list(csv.DictReader(io.StringIO(result.output)))
    assert rows[0]["gain"] == "150.00" and rows[0]["ticker"] == "UP.US"
    out = workdir / "g.csv"
    again = runner.invoke(app, ["tax", "gains", "--year", "2025", "--out", str(out)])
    assert again.exit_code == 0, again.output
    assert "150.00" in out.read_text()
    divs = runner.invoke(app, ["tax", "dividends", "--year", "2025"])
    assert divs.exit_code == 0 and divs.output.startswith("ticker,ex_date")


def test_tax_settings_show_and_set(runner, workdir):
    shown = runner.invoke(app, ["tax", "settings"])
    assert shown.exit_code == 0 and "base USD" in shown.output
    set_ = runner.invoke(app, ["tax", "settings", "--base-currency", "eur", "--no-wash-sales"])
    assert set_.exit_code == 0, set_.output
    assert "base EUR" in set_.output and "wash sales off" in set_.output
    bad = runner.invoke(app, ["tax", "settings", "--jurisdiction", "mars"])
    assert bad.exit_code != 0


def test_ingest_fx(runner, workdir, monkeypatch):
    source = _FakeFx({("EUR", "USD"): [(date(2025, 1, 2), 1.05)]})
    monkeypatch.setattr(cli, "_build_source", lambda settings, source_id: source)
    result = runner.invoke(app, ["ingest", "fx", "--pairs", "EURUSD,GBP/USD"])
    assert result.exit_code == 0, result.output
    assert "kind=fx status=partial ok=1 failed=1" in result.output
    lake = DuckDBLake(workdir / "data" / "lake.duckdb")
    assert len(lake.get_fx_rates()) == 1
    lake.close()
    bad = runner.invoke(app, ["ingest", "fx", "--pairs", "EURO"])
    assert bad.exit_code != 0

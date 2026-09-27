"""``stonks options ingest|chain|strategies|backtest`` over a temp lake,
with the synthetic source standing in for the vendor (hermetic)."""

from __future__ import annotations

from datetime import date, timedelta

import pandas as pd
import pytest
from typer.testing import CliRunner

from stonks.cli import app
from stonks.options.synthetic import SyntheticChainSpec, SyntheticOptionSource
from stonks.store.lake import DuckDBLake

DAYS = [date(2025, 1, 2) + timedelta(days=i) for i in range(40)]
DAYS = [d for d in DAYS if d.weekday() < 5]
CLOSES = {d: 100.0 + 0.3 * i for i, d in enumerate(DAYS)}


@pytest.fixture
def data_dir(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    data = tmp_path / "data"
    data.mkdir()
    monkeypatch.setenv("STONKS_DATA_DIR", str(data))
    monkeypatch.setenv("COLUMNS", "250")
    with DuckDBLake(data / "lake.duckdb") as lake:
        lake.migrate()
        lake.upsert_prices(
            pd.DataFrame(
                [
                    {
                        "ticker": "X.US",
                        "date": d,
                        "open": c,
                        "high": c,
                        "low": c,
                        "close": c,
                        "adj_close": c,
                        "volume": 1e6,
                    }
                    for d, c in CLOSES.items()
                ]
            )
        )
    src = SyntheticOptionSource(
        {"X.US": CLOSES},
        SyntheticChainSpec(horizon_days=50),
        fixed_strikes={"X.US": [float(k) for k in range(80, 125, 5)]},
    )
    monkeypatch.setattr("stonks.cli._build_source", lambda settings, source_id: src)
    return data


def run(*args: str):
    return CliRunner().invoke(app, ["options", *args])


def test_ingest_chain_strategies_and_backtest(data_dir):
    result = run("ingest", "--underlyings", "X.US", "--source", "synthetic")
    assert result.exit_code == 0, result.output
    assert "ok 1, failed 0" in result.output

    day = DAYS[5].isoformat()
    shown = run("chain", "X.US", "--as-of", day, "--expiry", "2025-01-17")
    assert shown.exit_code == 0, shown.output
    assert "X.US:2025-01-17:C:100" in shown.output

    listed = run("strategies")
    assert listed.exit_code == 0 and "covered_call" in listed.output

    bt = run(
        "backtest",
        "cash_secured_put",
        "--underlyings",
        "X.US",
        "--start",
        DAYS[0].isoformat(),
        "--end",
        DAYS[-1].isoformat(),
        "--validate",
    )
    assert bt.exit_code == 0, bt.output
    assert "cash_secured_put:" in bt.output and "fill_stress" in bt.output

    bad = run("backtest", "nope", "--underlyings", "X.US", "--start", day, "--end", day)
    assert bad.exit_code != 0


def test_ingest_reports_failures(data_dir):
    result = run("ingest", "--underlyings", "NOPE.US", "--source", "synthetic")
    assert result.exit_code == 0 and "failed 1" in result.output and "NOPE.US" in result.output

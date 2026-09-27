"""`stonks report --backtest forecast_blend` shows the forecast weights
section (roadmap 22.7)."""

from __future__ import annotations

from typer.testing import CliRunner

from stonks.cli import app
from stonks.store.state import SqliteState
from tests.unit.trend_helpers import DATES, LAST, build_lake, trend


def test_tear_sheet_has_the_forecast_weights_section(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    lake_path = tmp_path / "lake.duckdb"
    lake = build_lake(
        lake_path,
        {"UP.US": trend(0.002, seed=1), "FLAT.US": trend(0.0, seed=6)},
        asset_classes={"UP.US": "equity", "FLAT.US": "equity"},
    )
    lake.close()
    (tmp_path / "config").mkdir()
    (tmp_path / "config" / "default.toml").write_text(
        f"""
[lake]
path = "{lake_path.as_posix()}"

[state]
path = "state.sqlite"

[registry]
artifacts_dir = "artifacts"
""".strip()
    )
    with SqliteState(tmp_path / "state.sqlite") as state:
        state.migrate()
    out = tmp_path / "ts.html"
    args = ["report", "--backtest", "forecast_blend", "--tickers", "UP.US,FLAT.US",
            "--start", DATES[600].date().isoformat(), "--end", LAST.date().isoformat(),
            "--out", str(out)]  # fmt: skip
    result = CliRunner().invoke(app, args, catch_exceptions=False)
    assert result.exit_code == 0, result.output
    html = out.read_text(encoding="utf-8")
    assert "Forecast weights" in html
    assert "UP.US" in html and "ewmac64" in html
    assert "SR after costs" in html

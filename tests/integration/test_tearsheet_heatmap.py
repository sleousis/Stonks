"""A registered strategy's tear sheet shows the parameter heatmap its lab
run stored in ``meta.json`` (22.5)."""

from __future__ import annotations

import pytest
from typer.testing import CliRunner

from stonks.cli import app
from stonks.lab.heatmap import ParameterHeatmap
from stonks.registry.artifact import update_meta
from stonks.registry.store import StrategyRegistry
from stonks.store.state import SqliteState
from stonks.strategies.examples.buy_and_hold import BuyAndHold

WINDOW = ["--start", "2025-10-01", "--end", "2026-04-01"]


@pytest.fixture
def env(tmp_path, monkeypatch, lake_trending):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "config").mkdir()
    lake_path = (tmp_path / "lake.duckdb").as_posix()
    (tmp_path / "config" / "default.toml").write_text(
        f'[lake]\npath = "{lake_path}"\n\n[state]\npath = "state.sqlite"\n\n'
        '[registry]\nartifacts_dir = "artifacts"\n'
    )
    with SqliteState(tmp_path / "state.sqlite") as state:
        state.migrate()
    return tmp_path


def _register(env, heatmap: ParameterHeatmap | None) -> str:
    with SqliteState(env / "state.sqlite") as state:
        registry = StrategyRegistry(state=state, artifacts_dir=env / "artifacts")
        sid = registry.register(BuyAndHold({"ticker": "UP.US"}), [], strategy_id="bah_up")
        if heatmap is not None:
            [handle] = [h for h in registry.list_all() if h.id == sid]
            update_meta(handle.artifact_path, {"heatmap": heatmap.to_dict()})
    return sid


def _tear_sheet(env, sid: str) -> str:
    out = env / "ts.html"
    result = CliRunner().invoke(
        app,
        ["report", "--backtest", sid, "--tickers", "UP.US", *WINDOW, "--out", str(out)],
        catch_exceptions=False,
    )
    assert result.exit_code == 0, result.output
    return out.read_text(encoding="utf-8")


def test_the_heatmap_from_the_lab_run_is_on_the_tear_sheet(env):
    heatmap = ParameterHeatmap(
        x="a",
        y="b",
        x_values=[1, 2],
        y_values=[0.1, 0.2],
        scores=[[0.5, float("nan")], [1.5, 2.5]],
        metric="sharpe",
        fast=False,
        best={"a": 2, "b": 0.2},
        fixed={},
    )
    html = _tear_sheet(env, _register(env, heatmap))
    assert "Parameter heatmap" in html
    assert "2.5 (tuned)" in html


def test_no_heatmap_section_without_one(env):
    assert "Parameter heatmap" not in _tear_sheet(env, _register(env, None))

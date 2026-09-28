"""CLI tests for ``stonks screener`` (roadmap 20.8): metrics, a run, saved
screens and a screen stored as a universe."""

from __future__ import annotations

import json

import pytest
from typer.testing import CliRunner

from stonks.cli import app
from stonks.store.lake import DuckDBLake
from tests.fixtures.screener import END, seed_market

CONFIG = """
[lake]
path = "data/lake.duckdb"

[state]
path = "data/state.sqlite"

[registry]
artifacts_dir = "data/artifacts"
""".strip()

AS_OF = END.isoformat()


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
    lake = DuckDBLake(tmp_path / "data" / "lake.duckdb")
    lake.migrate()
    seed_market(lake)
    lake.close()
    return tmp_path


def test_metrics(runner, workdir):
    res = runner.invoke(app, ["screener", "metrics"])
    assert res.exit_code == 0, res.output
    assert "pe_ratio" in res.output and "return_12m" in res.output


def test_run_save_list_delete(runner, workdir):
    spec = json.dumps({"sectors": ["Tech"], "sort_by": "price"})
    res = runner.invoke(app, ["screener", "run", "--spec", spec, "--as-of", AS_OF])
    assert res.exit_code == 0, res.output
    assert res.output.index("BBB.US") < res.output.index("AAA.US")
    assert "CCC.US" not in res.output
    saved = runner.invoke(app, ["screener", "save", "Tech", "--spec", spec])
    assert saved.exit_code == 0, saved.output
    sid = saved.output.split()[1]
    listed = runner.invoke(app, ["screener", "list"])
    assert sid in listed.output
    again = runner.invoke(app, ["screener", "run", "--screen", sid, "--as-of", AS_OF])
    assert "BBB.US" in again.output
    assert runner.invoke(app, ["screener", "delete", sid]).exit_code == 0
    assert "no saved screens" in runner.invoke(app, ["screener", "list"]).output


def test_bad_input_is_a_usage_error(runner, workdir):
    assert runner.invoke(app, ["screener", "run", "--spec", "{"]).exit_code == 2
    assert runner.invoke(app, ["screener", "run", "--spec", "[]"]).exit_code == 2
    assert runner.invoke(app, ["screener", "run"]).exit_code == 2
    bad_metric = json.dumps({"sort_by": "nope"})
    assert runner.invoke(app, ["screener", "run", "--spec", bad_metric]).exit_code == 2
    assert runner.invoke(app, ["screener", "delete", "scr_missing"]).exit_code == 2


def test_universe_from_a_screen(runner, workdir):
    spec = json.dumps({"filters": [{"metric": "return_3m", "min": 0.01}]})
    res = runner.invoke(
        app,
        ["screener", "universe", "rising", "--spec", spec, "--start", "2024-06-03", "--end", AS_OF],
    )
    assert res.exit_code == 0, res.output
    assert "stored rule universe rising" in res.output
    assert "1 members" in res.output
    snap = runner.invoke(
        app,
        ["screener", "universe", "snap", "--spec", "{}", "--mode", "snapshot", "--no-refresh"],
    )
    # no metric filter: every listed name today, stored as a fixed list
    assert snap.exit_code == 0, snap.output
    assert "survivorship" in snap.output


def test_screen_alerts_from_the_shell(runner, workdir):
    """Roadmap 23.17: alert on a saved screen, run it twice, read the event."""
    spec = json.dumps({"filters": [{"metric": "price", "min": 19.5}]})
    saved = runner.invoke(app, ["screener", "save", "Pricey", "--spec", spec])
    assert saved.exit_code == 0, saved.output
    sid = saved.output.split()[1]
    bad = runner.invoke(app, ["screener", "alert", sid, "--weekly", "someday"])
    assert bad.exit_code != 0
    on = runner.invoke(app, ["screener", "alert", sid, "--weekly", "fri"])
    assert on.exit_code == 0 and "weekly on fri, on" in on.output, on.output
    on = runner.invoke(app, ["screener", "alert", sid])
    assert "daily, on" in on.output
    listed = runner.invoke(app, ["screener", "alerts"])
    assert "Pricey" in listed.output and "daily" in listed.output
    first = runner.invoke(app, ["screener", "alerts-run", "--as-of", "2024-02-01"])
    assert "1 baselines" in first.output, first.output
    second = runner.invoke(app, ["screener", "alerts-run", "--as-of", AS_OF])
    assert "1 found new names" in second.output, second.output
    events = runner.invoke(app, ["screener", "alert-events"])
    assert "AAA.US" in events.output and "Pricey" in events.output
    gone = runner.invoke(app, ["screener", "alert-delete", sid])
    assert gone.exit_code == 0
    assert "no screen alerts" in runner.invoke(app, ["screener", "alerts"]).output

"""CLI tests for `stonks factors`: list, show, check, values, tearsheet and
dataset."""

from __future__ import annotations

import json

import pytest
from typer.testing import CliRunner

from stonks.cli import app
from tests.integration.app.test_factors_service import TICKERS, seed_factor_lake

CONFIG = """
[lake]
path = "data/lake.duckdb"

[state]
path = "data/state.sqlite"

[registry]
artifacts_dir = "data/artifacts"

[factors]
cache = "memory"
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
    seed_factor_lake(tmp_path / "data" / "lake.duckdb")
    return tmp_path


def test_list_and_show(runner, workdir):
    out = runner.invoke(app, ["factors", "list", "--set", "classic"])
    assert out.exit_code == 0, out.output
    assert "mom_12_1" in out.output and "alpha158 (157)" in out.output
    shown = runner.invoke(app, ["factors", "show", "low_vol_60"])
    assert shown.exit_code == 0 and "direction: -1" in shown.output
    missing = runner.invoke(app, ["factors", "show", "nope"])
    assert missing.exit_code != 0


def test_check(runner, workdir):
    ok = runner.invoke(app, ["factors", "check", "Mean($close, 5)/$close"])
    assert ok.exit_code == 0 and "warm-up 4 bars" in ok.output
    bad = runner.invoke(app, ["factors", "check", "Ref($close, -1)"])
    assert bad.exit_code == 1 and "future" in bad.output


def test_values(runner, workdir):
    out = runner.invoke(
        app, ["factors", "values", "ROC20", "--as-of", "2026-03-02", "--tickers", ",".join(TICKERS)]
    )
    assert out.exit_code == 0, out.output
    assert "FAC00.US" in out.output
    as_json = runner.invoke(
        app,
        ["factors", "values", "KMID", "--as-of", "2026-03-02", "--universe-id", "fac", "--json"],
    )
    assert as_json.exit_code == 0, as_json.output
    assert len(json.loads(as_json.output)["values"]) == 11
    neither = runner.invoke(app, ["factors", "values", "KMID", "--as-of", "2026-03-02"])
    assert neither.exit_code != 0
    bad_day = runner.invoke(
        app, ["factors", "values", "KMID", "--as-of", "03/02", "--tickers", "A"]
    )
    assert bad_day.exit_code != 0


def test_tearsheet_writes_json_and_html(runner, workdir):
    args = [
        "factors", "tearsheet", "mom_6_1", "--tickers", ",".join(TICKERS),
        "--start", "2025-09-01", "--end", "2026-03-31", "--horizons", "1,5",
        "--json", "sheet.json", "--html", "sheet.html",
    ]  # fmt: skip
    out = runner.invoke(app, args)
    assert out.exit_code == 0, out.output
    assert "IC by sector" in out.output
    sheet = json.loads((workdir / "sheet.json").read_text())
    assert sheet["status"] == "ok"
    assert "Monthly IC" in (workdir / "sheet.html").read_text()
    small = runner.invoke(
        app,
        ["factors", "tearsheet", "KMID", "--tickers", "FAC00.US,FAC01.US",
         "--start", "2025-09-01", "--end", "2026-03-31"],
    )  # fmt: skip
    assert small.exit_code == 0 and "n/a" in small.output
    bad = runner.invoke(
        app,
        ["factors", "tearsheet", "KMID", "--tickers", "A", "--start", "2026-01-01",
         "--end", "2026-02-01", "--horizons", "x"],
    )  # fmt: skip
    assert bad.exit_code != 0


def test_dataset_export(runner, workdir):
    out = runner.invoke(
        app,
        ["factors", "dataset", "KMID,ROC5", "--universe-id", "fac", "--start", "2026-01-05",
         "--end", "2026-03-31", "--out", "data.csv"],
    )  # fmt: skip
    assert out.exit_code == 0, out.output
    header = (workdir / "data.csv").read_text().splitlines()[0]
    assert header == "timestamp,ticker,KMID,ROC5,label"
    wrong = runner.invoke(
        app,
        ["factors", "dataset", "KMID", "--tickers", "A", "--start", "2026-01-05",
         "--end", "2026-03-31", "--out", "data.xlsx"],
    )  # fmt: skip
    assert wrong.exit_code != 0
    bad = runner.invoke(
        app,
        ["factors", "dataset", "Ref($close,-1)", "--tickers", "A", "--start", "2026-01-05",
         "--end", "2026-03-31", "--out", "d.csv", "--no-label"],
    )  # fmt: skip
    assert bad.exit_code != 0


def test_bench_labels_factors_and_counts_trials(runner, workdir):
    args = [
        "factors", "bench", "classic,setups", "--tickers", ",".join(TICKERS),
        "--start", "2025-09-01", "--end", "2026-03-31", "--horizon", "5",
    ]  # fmt: skip
    out = runner.invoke(app, [*args, "--json", "bench.json"])
    assert out.exit_code == 0, out.output
    assert "factors benched so far" in out.output
    result = json.loads((workdir / "bench.json").read_text())
    assert len(result["rows"]) == 13
    assert {r["label"] for r in result["rows"]} <= {"alive", "reversed", "dead", "n/a"}
    again = runner.invoke(app, [*args, "--json", "bench2.json"])
    assert again.exit_code == 0, again.output
    assert json.loads((workdir / "bench2.json").read_text())["n_trials_family"] == 26
    dry = runner.invoke(app, [*args, "--no-record", "--json", "bench3.json"])
    assert dry.exit_code == 0, dry.output
    assert json.loads((workdir / "bench3.json").read_text())["run_id"] is None

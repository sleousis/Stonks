"""`stonks lab run` with the Optuna tuner and a parameter heatmap (22.1, 22.5)."""

from __future__ import annotations

import json

import pytest
import typer

from stonks.cli import _heatmap_option
from tests.integration.test_cli_lab import UNIVERSE, WINDOW, _run, lab_env, runner  # noqa: F401


def test_optuna_with_a_heatmap_prints_the_grid_and_saves_it(runner, lab_env):  # noqa: F811
    out = lab_env / "result.json"
    r = _run(
        runner,
        "momentum",
        *UNIVERSE,
        *WINDOW,
        "--tuner", "optuna", "--sampler", "tpe", "--prune", "--budget", "3",
        "--objective", "calmar", "--tests", "plateau",
        "--heatmap", "lookback_days,threshold", "--heatmap-grid", "2",
        "--json-out", str(out),
    )  # fmt: skip
    assert r.exit_code == 0, r.output
    assert "heatmap:" in r.output and "plateau test:" in r.output
    doc = json.loads(out.read_text())
    heatmap = doc["heatmap"]
    assert (heatmap["x"], heatmap["y"]) == ("lookback_days", "threshold")
    assert heatmap["fast"] is True
    cells = len(heatmap["x_values"]) * len(heatmap["y_values"])
    assert doc["n_trials_run"] == 3 + cells


def test_no_heatmap_flag_saves_none(runner, lab_env):  # noqa: F811
    out = lab_env / "result.json"
    r = _run(
        runner, "momentum", *UNIVERSE, *WINDOW,
        "--tuner", "grid", "--grid-size", "2", "--budget", "4", "--json-out", str(out),
    )  # fmt: skip
    assert r.exit_code == 0, r.output
    assert json.loads(out.read_text())["heatmap"] is None


def test_heatmap_option_parsing():
    assert _heatmap_option(None, 7, False) is None
    auto = _heatmap_option("AUTO", 5, True)
    assert (auto.x, auto.y, auto.grid_size, auto.fast) == (None, None, 5, False)
    pair = _heatmap_option(" a , b ", 3, False)
    assert (pair.x, pair.y, pair.fast) == ("a", "b", True)
    with pytest.raises(typer.BadParameter):
        _heatmap_option("a", 3, False)

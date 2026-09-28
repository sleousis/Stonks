"""The parameter heatmap in the tear sheet and the terminal (22.5)."""

from __future__ import annotations

from datetime import date, timedelta

from stonks.backtest.report import compute_report
from stonks.core.params import ParameterSpec
from stonks.core.protocols import SurvivalReport
from stonks.lab.heatmap import FAST_METRIC, ParameterHeatmap, plateau_overlay
from stonks.reporting.heatmap import heatmap_html, heatmap_text
from stonks.reporting.tearsheet import TearSheet, render_tear_sheet

_SPACE = [
    ParameterSpec(name="a", kind="int", default=5, bounds=(0, 10)),
    ParameterSpec(name="b", kind="float", default=0.5, bounds=(0.0, 1.0)),
]


def _map(metric: str = "sharpe") -> ParameterHeatmap:
    return ParameterHeatmap(
        x="a",
        y="b",
        x_values=[0, 5, 10],
        y_values=[0.0, 0.5, 1.0],
        scores=[[-1.0, 2.0, float("nan")], [3.0, 4.0, 5.0], [6.0, 7.0, 8.0]],
        metric=metric,
        fast=metric == FAST_METRIC,
        best={"a": 5, "b": 0.5},
        fixed={"<c>": 1},
    )


def test_html_marks_the_tuned_cell_the_plateau_and_the_verdict():
    report = SurvivalReport("plateau", False, {}, "train ratio 0.2 < 0.7")
    html = heatmap_html(plateau_overlay(_map(), report, _SPACE, step=0.2))
    assert "Parameter heatmap" in html
    assert "FAIL" in html and "train ratio 0.2 &lt; 0.7" in html
    assert "4 (tuned)" in html
    assert html.count("2px dashed") == 1  # only the tuned cell is within 20% on both axes
    assert "n/a" in html  # the failed cell
    assert "&lt;c&gt;=1" in html  # data is escaped
    assert "<script" not in html


def test_html_without_a_plateau_report_says_so_and_names_the_fast_metric():
    html = heatmap_html(_map(FAST_METRIC))
    assert "did not run" in html
    assert "fast Sharpe" in html


def test_text_stars_the_tuned_cell_and_prints_the_verdict():
    report = SurvivalReport("plateau", True, {}, "tuned params sit on a plateau")
    text = heatmap_text(plateau_overlay(_map(), report, _SPACE, step=0.2))
    assert "4*" in text
    assert "plateau test: pass" in text
    assert "n/a" in text


def test_the_tear_sheet_shows_the_heatmap_when_given():
    start = date(2024, 1, 1)
    curve = [100.0 * (1.001**i) for i in range(60)]
    report = compute_report("s", [start + timedelta(days=i) for i in range(60)], curve)
    assert "Parameter heatmap" not in render_tear_sheet(TearSheet("t", report))
    assert "Parameter heatmap" in render_tear_sheet(TearSheet("t", report, heatmap=_map()))

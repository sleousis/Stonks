"""Learning ranker tear sheet section (roadmap 23.12)."""

from __future__ import annotations

from stonks.features.ranking import RankerReport
from stonks.reporting.ranker import TOP_FEATURES, ranker_sections, render_ranker_section
from stonks.reporting.sections import strategy_report_sections

STATE = {
    "model": "hist_gbm",
    "hyperparameters": {"learning_rate": 0.05, "max_iter": 200},
    "feature_names": [f"f{i}" for i in range(30)],
    "horizon_bars": 21,
    "n_rows": 1200,
    "n_dates": 80,
    "train_start": "2023-01-02",
    "train_end": "2024-06-28",
    "cv_folds": 3,
    "ic_mean": 0.041,
    "ic_std": 0.1,
    "ic_ir": 0.41,
    "ic_hit_rate": 0.6,
    "importance": {f"f{i}": 0.001 * i for i in range(30)},
    "ic_by_date": {"2024-01-05": 0.1, "2024-01-12": 0.0, "2024-02-02": -0.2},
}


class _Ranker:
    def __init__(self, state):
        self.state = state

    def ranker_report(self):
        return None if self.state is None else RankerReport.from_state(self.state)


class _Wrapper:
    def __init__(self, inner):
        self.inner = inner


def test_section_shows_ic_importance_and_months():
    html = render_ranker_section(RankerReport.from_state(STATE))
    assert "Learning ranker" in html and "+0.041" in html and "60%" in html
    assert "f29" in html and f"f{29 - TOP_FEATURES}" not in html  # the top 20 only
    assert html.index("f29") < html.index("f28")  # most important first
    assert "2024-01" in html and "+0.050" in html and "-0.200" in html


def test_section_escapes_feature_names():
    state = {**STATE, "importance": {"<b>x</b>": 0.1}}
    html = render_ranker_section(RankerReport.from_state(state))
    assert "<b>x</b>" not in html and "&lt;b&gt;" in html


def test_section_without_the_diagnostic_says_so():
    state = {k: v for k, v in STATE.items() if not k.startswith("ic")} | {"importance": {}}
    html = render_ranker_section(RankerReport.from_state(state))
    assert "diagnostic was off" in html and "no out-of-fold IC" in html


def test_sections_find_a_fitted_ranker_through_wrappers():
    assert len(ranker_sections(_Ranker(STATE))) == 1
    assert len(ranker_sections(_Wrapper(_Ranker(STATE)))) == 1
    assert ranker_sections(_Ranker(None)) == []
    assert ranker_sections(object()) == []
    assert len(strategy_report_sections(_Ranker(STATE))) == 1
    assert strategy_report_sections(object()) == []

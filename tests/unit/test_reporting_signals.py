"""HTML sections for signal research (BL-33, BL-34)."""

from __future__ import annotations

import math

from stonks.lab.signal_eval import HorizonIC, SignalICResult
from stonks.lab.survival.event_study import EventGroup, EventStudyResult, HorizonEvents
from stonks.reporting.signals import (
    render_event_study_section,
    render_signal_ic_section,
    render_signal_page,
)


def _ic(**kw) -> SignalICResult:
    h = HorizonIC(
        horizon=5,
        n_dates=40,
        mean_ic=0.034,
        ic_std=0.1,
        icir=0.34,
        hit_rate=0.6,
        se_iid=0.01,
        se_hac=0.02,
        hac_lags=4,
        t_stat_hac=1.7,
        quantile_means=[-0.01, 0.0, 0.002, 0.004, 0.012],
        spread_mean=0.022,
        spread_t_hac=math.inf,
    )
    base = {
        "strategy_id": "<b>mom</b>",
        "window": ("2024-01-01", "2024-12-31"),
        "n_tickers": 12,
        "n_dates": 50,
        "every_bars": 5,
        "n_quantiles": 5,
        "status": "ok",
        "horizons": [h],
        "score_turnover": 0.2,
        "top_quantile_turnover": 0.1,
        "ic_horizon": 5,
        "ic_estimate": 0.034,
    }
    return SignalICResult(**{**base, **kw})


def _events() -> EventStudyResult:
    h = HorizonEvents(20, 60, 0.03, 0.01, 0.02, 0.01, 0.03, 0.002)
    return EventStudyResult(
        strategy_id="mom",
        window=("2024-01-01", "2024-12-31"),
        holding_bars=20,
        holding_source="ledger",
        alpha=0.05,
        n_boot=1000,
        seed=17,
        n_events=60,
        groups=[EventGroup("all", 4, 60, [h]), EventGroup("equity", 4, 60, [h])],
    )


def test_ic_section_escapes_and_lists_horizons():
    html = render_signal_ic_section(_ic())
    assert "&lt;b&gt;mom&lt;/b&gt;" in html and "<b>mom</b>" not in html
    assert "<td>5</td>" in html
    assert "+0.0340" in html  # mean IC
    assert "Q5" in html
    assert "+∞" in html  # an exact spread has an infinite t


def test_ic_section_for_na():
    html = render_signal_ic_section(_ic(status="n/a", note="too small", horizons=[]))
    assert "too small" in html
    assert "<table" not in html


def test_event_section_marks_each_group():
    html = render_event_study_section(_events())
    assert "equity" in html
    assert "PASS" in html
    assert "ledger" in html


def test_page_is_self_contained():
    page = render_signal_page(_ic(), _events())
    assert page.startswith("<!doctype html>")
    assert "<script" not in page
    assert "Event study" in page and "Signal IC" in page
    assert "Event study" not in render_signal_page(_ic())

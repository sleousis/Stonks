"""Event study against baseline drift (BL-34)."""

from __future__ import annotations

import json

import numpy as np
import pytest

from stonks.lab.survival.event_study import EventStudyTest, entry_events, event_study
from stonks.lab.survival.registry import build_survival_test, survival_test_names
from tests.fixtures.signal_research import (
    ClassSplitSignal,
    FutureReturnSignal,
    NoiseSignal,
    dataset_for,
    signal_lake,
)

TICKERS = ["EQA", "EQB", "EQC", "EQD"]


@pytest.fixture(scope="module")
def lake():
    lake = signal_lake(":memory:", TICKERS, periods=300, seed=11)
    yield lake
    lake.close()


@pytest.fixture(scope="module")
def mixed_lake():
    tickers = ["EQA", "EQB", "CRA", "CRB"]
    lake = signal_lake(
        ":memory:",
        tickers,
        periods=300,
        seed=12,
        asset_classes={"CRA": "crypto", "CRB": "crypto"},
    )
    yield lake, tickers
    lake.close()


def _test(**options) -> EventStudyTest:
    base = {"window": "full", "holding_bars": 20, "min_events": 30, "max_workers": 1}
    return build_survival_test("event_study", {**base, **options})  # type: ignore[return-value]


def test_registered():
    assert "event_study" in survival_test_names()


def test_entry_events_are_new_picks_only():
    picks = np.array([False, True, True, False, True, True, True, False])
    assert entry_events(picks).tolist() == [1, 4]
    # a pick on the first bar has no prior state: not an event
    assert entry_events(np.array([True, True, False, True])).tolist() == [3]


def test_planted_edge_passes(lake):
    report = _test().run(
        FutureReturnSignal({"horizon": 20, "threshold": 0.03}), dataset_for(lake, TICKERS)
    )
    assert report.passed, report.notes
    assert report.metrics["excess_h20"] > 0
    assert report.metrics["p_value_h20"] <= 0.05
    assert report.metrics["ci_low_h20"] > 0
    assert report.metrics["n_events"] >= 30
    assert report.metrics["holding_bars"] == 20


def test_random_entry_fails(lake):
    report = _test().run(NoiseSignal({"seed": 3}), dataset_for(lake, TICKERS))
    assert not report.passed
    assert report.metrics["p_value_h20"] > 0.05
    assert report.metrics["ci_low_h20"] < 0 < report.metrics["ci_high_h20"]


def test_reports_per_asset_class_and_fails_on_a_class_without_edge(mixed_lake):
    lake, tickers = mixed_lake
    report = _test().run(ClassSplitSignal({}), dataset_for(lake, tickers))
    assert not report.passed
    assert report.metrics["equity.excess_h20"] > 0
    assert report.metrics["equity.p_value_h20"] <= 0.05
    assert report.metrics["crypto.p_value_h20"] > 0.05
    assert "crypto" in report.notes


def test_too_few_events_fails_with_a_note(lake):
    report = _test(min_events=500).run(
        FutureReturnSignal({"horizon": 20, "threshold": 0.03}), dataset_for(lake, TICKERS)
    )
    assert not report.passed
    assert "fewer than 500" in report.notes


def test_holding_horizon_defaults_to_the_trade_ledger(lake):
    report = _test(holding_bars=None, min_events=30).run(
        FutureReturnSignal({"horizon": 20, "threshold": 0.03}), dataset_for(lake, TICKERS)
    )
    assert report.metrics["holding_bars"] >= 1
    h = int(report.metrics["holding_bars"])
    assert f"excess_h{h}" in report.metrics


def test_validation_window_is_the_default(lake):
    test = build_survival_test("event_study", {"holding_bars": 5, "max_workers": 1})
    ds = dataset_for(lake, TICKERS)
    report = test.run(FutureReturnSignal({"horizon": 5}), ds)
    assert str(ds.val_window[0]) in report.notes


def test_seeded_and_plain_data(lake):
    ds = dataset_for(lake, TICKERS)
    a = event_study(
        FutureReturnSignal({"horizon": 20, "threshold": 0.03}), ds, ds.full_window, holding_bars=20
    )
    b = event_study(
        FutureReturnSignal({"horizon": 20, "threshold": 0.03}), ds, ds.full_window, holding_bars=20
    )
    assert a.to_dict() == b.to_dict()
    json.dumps(a.to_dict())
    assert [g.asset_class for g in a.groups] == ["all", "equity"]


def test_rejects_bad_options():
    with pytest.raises(ValueError):
        build_survival_test("event_study", {"min_events": 10})
    with pytest.raises(ValueError):
        build_survival_test("event_study", {"alpha": 0.5})
    with pytest.raises(ValueError):
        build_survival_test("event_study", {"horizons": []})


def test_bootstrap_block_covers_the_events_inside_one_horizon():
    from stonks.lab.survival.event_study import event_block_length

    # 400 events over 100 bars: an h=5 window holds about 20 overlapping events
    assert event_block_length(5, n_events=400, n_bars=100) == pytest.approx(20.0)
    # sparse events barely overlap: blocks of one event
    assert event_block_length(5, n_events=10, n_bars=1000) == 1.0
    # never longer than the sample
    assert event_block_length(60, n_events=30, n_bars=40) == 30.0


def test_events_without_a_forward_return_do_not_count(monkeypatch):
    # RS-27: 40 entries, but only 10 have a holding-horizon forward return
    # (the rest sit in the last h bars); 10 < min_events must fail
    from stonks.lab.survival import event_study as es

    h = 20
    good = es.HorizonEvents(h, 10, 0.05, 0.0, 0.05, 0.01, 0.09, 0.001)
    result = es.EventStudyResult(
        strategy_id="x",
        window=("2024-01-01", "2024-12-31"),
        holding_bars=h,
        holding_source="option",
        alpha=0.05,
        n_boot=100,
        seed=1,
        n_events=40,
        groups=[es.EventGroup("all", 4, 40, [good]), es.EventGroup("equity", 4, 40, [good])],
    )
    monkeypatch.setattr(es, "event_study", lambda *a, **k: result)
    monkeypatch.setattr(es, "scoring_window", lambda *a, **k: (None, None))
    report = _test(min_events=30).run(object(), object())
    assert not report.passed
    assert "fewer than 30 events" in report.notes

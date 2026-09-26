"""Cross-instrument consistency test (BL-19)."""

from __future__ import annotations

import pytest

from stonks.lab.survival.cross_instrument import CrossInstrumentOptions, CrossInstrumentTest
from stonks.lab.survival.registry import build_survival_test, survival_test_names
from stonks.strategies.examples.buy_and_hold import BuyAndHold
from tests.fixtures.robustness_lab import LongAll, PeakTrader, dataset_for, trend_lake

_UP = (0.002, 0.003)
_DOWN = (-0.002, 0.003)


def _run(trends, universe, strategy=None, lake_path=":memory:", **kw):
    lake = trend_lake(lake_path, trends, asset_classes=kw.pop("asset_classes", None))
    try:
        test = CrossInstrumentTest(**{"max_workers": 1, **kw})
        return test.run(strategy or LongAll({}), dataset_for(lake, universe))
    finally:
        lake.close()


def test_registered_under_its_id_with_spec_defaults():
    assert "cross_instrument" in survival_test_names()
    assert isinstance(build_survival_test("cross_instrument", {}), CrossInstrumentTest)
    o = CrossInstrumentOptions()
    assert (o.min_positive_share, o.max_pnl_share, o.min_tickers) == (0.6, 0.5, 3)
    with pytest.raises(ValueError):
        CrossInstrumentOptions(min_positive_share=0.9)


def test_consistent_edge_passes():
    trends = dict.fromkeys(("A.US", "B.US", "C.US", "D.US"), _UP)
    report = _run(trends, list(trends))
    assert report.passed, report.notes
    assert report.metrics["n_tickers"] == 4
    assert report.metrics["positive_share"] == 1.0
    assert report.metrics["max_pnl_share"] < 0.5


def test_single_ticker_dominated_fails():
    trends = {"DOM.US": (0.006, 0.004), **dict.fromkeys(("B.US", "C.US", "D.US"), (0.0002, 0.001))}
    report = _run(trends, list(trends))
    assert not report.passed
    assert report.metrics["max_pnl_share"] > 0.5
    assert "DOM.US" in report.notes


def test_mostly_losing_tickers_fail():
    trends = {"A.US": _UP, "B.US": _DOWN, "C.US": _DOWN, "D.US": _DOWN}
    report = _run(trends, list(trends))
    assert not report.passed
    assert report.metrics["positive_share"] == 0.25
    assert "positive expectancy" in report.notes


def test_small_universe_is_not_applicable():
    report = _run({"A.US": _UP, "B.US": _DOWN}, ["A.US", "B.US"])
    assert report.passed
    assert "n/a" in report.notes
    assert report.metrics["n_tickers"] == 2


def test_no_trades_anywhere_is_insufficient_data():
    trends = dict.fromkeys(("A.US", "B.US", "C.US"), _UP)
    report = _run(trends, list(trends), strategy=PeakTrader({"a": 3}))
    assert not report.passed
    assert "insufficient data" in report.notes


def test_single_ticker_strategies_get_each_ticker():
    trends = dict.fromkeys(("A.US", "B.US", "C.US"), _UP)
    report = _run(trends, list(trends), strategy=BuyAndHold({"ticker": "A.US"}))
    assert report.passed, report.notes
    assert report.metrics["positive_share"] == 1.0


def test_held_out_tickers_of_the_same_asset_class_join_the_check():
    trends = {"A.US": _UP, "B.US": _UP, "H1.US": _DOWN, "H2.US": _DOWN, "BTC.CC": _UP}
    report = _run(
        trends,
        ["A.US", "B.US"],
        held_out=["H1.US", "H2.US", "BTC.CC"],
        asset_classes={"BTC.CC": "crypto"},
    )
    assert report.metrics["n_tickers"] == 4
    assert report.metrics["n_held_out"] == 2
    assert "BTC.CC" in report.notes  # dropped: another asset class
    assert not report.passed  # the held-out tickers lose


def test_auto_held_out_picks_same_class_tickers_in_id_order():
    trends = {"A.US": _UP, "B.US": _UP, "C.US": _UP, "D.US": _UP, "Z.US": _UP, "BTC.CC": _UP}
    report = _run(trends, ["A.US", "B.US"], held_out_auto=2, asset_classes={"BTC.CC": "crypto"})
    assert report.metrics["n_held_out"] == 2
    assert "C.US" in report.notes and "D.US" in report.notes
    assert "Z.US" not in report.notes


def test_result_does_not_depend_on_worker_count(tmp_path):
    trends = {"A.US": _UP, "B.US": _UP, "C.US": _DOWN}
    serial = _run(trends, list(trends), lake_path=tmp_path / "a.duckdb", max_workers=1)
    pooled = _run(trends, list(trends), lake_path=tmp_path / "b.duckdb", max_workers=2)
    assert serial == pooled

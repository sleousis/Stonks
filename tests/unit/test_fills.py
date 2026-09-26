"""Unit tests for the ``FillModel`` seam (``stonks.backtest.fills``)."""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np
import pandas as pd
import pytest

from stonks.backtest.fills import (
    BarFillModel,
    BarQuote,
    ExecutionSettings,
    FillModel,
    FillModelSettings,
    ImmediateFillModel,
    MarketStatsSpec,
    lagged_market_stats,
)
from stonks.core.types import Order


def _order(side="buy", qty=100.0, kind="market", limit=None) -> Order:
    return Order(
        client_id="c1", ticker="X.US", side=side, quantity=qty, order_type=kind, limit_price=limit
    )


def _quote(**kw) -> BarQuote:
    base = {"open": 100.0, "high": 105.0, "low": 95.0, "volume": 10_000.0}
    base.update(kw)
    return BarQuote(**base)


def _model(**kw) -> BarFillModel:
    return FillModelSettings(**kw).build()


# ---- legacy ---------------------------------------------------------------------------


def test_models_satisfy_the_protocol():
    assert isinstance(ImmediateFillModel(), FillModel)
    assert isinstance(_model(), FillModel)


def test_immediate_model_fills_everything_at_the_open_whatever_the_type():
    d = ImmediateFillModel().decide(_order(kind="limit", limit=1.0), _quote(volume=0.0))
    assert (d.quantity, d.price, d.carry) == (100.0, 100.0, 0.0)


def test_execution_settings_default_to_the_legacy_model():
    assert isinstance(ExecutionSettings().fill_model(), ImmediateFillModel)
    assert isinstance(ExecutionSettings(fill=FillModelSettings()).fill_model(), BarFillModel)


# ---- participation cap ----------------------------------------------------------------


def test_participation_cap_clips_to_ten_percent_of_bar_volume_and_carries_the_rest():
    d = _model().decide(_order(qty=1_500.0), _quote(volume=10_000.0))
    assert d.quantity == pytest.approx(1_000.0)
    assert d.carry == pytest.approx(500.0)
    assert d.price == 100.0
    assert d.reason == "participation"


def test_order_under_the_cap_fills_in_full():
    d = _model().decide(_order(qty=900.0), _quote(volume=10_000.0))
    assert (d.quantity, d.carry) == (900.0, 0.0)


def test_no_carry_when_disabled():
    d = _model(carry_unfilled=False).decide(_order(qty=1_500.0), _quote(volume=10_000.0))
    assert (d.quantity, d.carry) == (pytest.approx(1_000.0), 0.0)


def test_missing_bar_volume_falls_back_to_adv():
    d = _model().decide(_order(qty=1_500.0), _quote(volume=None, adv=5_000.0))
    assert d.quantity == pytest.approx(500.0)
    assert d.carry == pytest.approx(1_000.0)


def test_adv_basis_ignores_bar_volume():
    d = _model(participation_basis="adv").decide(
        _order(qty=1_500.0), _quote(volume=1_000_000.0, adv=5_000.0)
    )
    assert d.quantity == pytest.approx(500.0)


def test_no_volume_and_no_adv_is_uncapped():
    d = _model().decide(_order(qty=1e9), _quote(volume=None, adv=None))
    assert d.quantity == 1e9


def test_cap_can_be_disabled():
    d = _model(max_participation=None).decide(_order(qty=1e6), _quote(volume=10.0))
    assert d.quantity == 1e6


def test_zero_volume_bar_never_fills_and_carries_the_order():
    d = _model().decide(_order(qty=10.0), _quote(volume=0.0))
    assert d.quantity == 0.0 and d.price is None
    assert d.carry == 10.0
    assert d.reason == "zero_volume"


def test_zero_volume_bar_may_be_allowed():
    d = _model(allow_zero_volume=True).decide(_order(qty=10.0), _quote(volume=0.0, adv=None))
    assert d.quantity == 10.0


# ---- limit and stop -------------------------------------------------------------------


@pytest.mark.parametrize(
    ("side", "kind", "level", "open_", "high", "low", "price"),
    [
        # buy limit: fills if low <= L at min(open, L)
        ("buy", "limit", 97.0, 100.0, 105.0, 95.0, 97.0),
        ("buy", "limit", 102.0, 100.0, 105.0, 95.0, 100.0),  # gap below the limit
        ("buy", "limit", 94.0, 100.0, 105.0, 95.0, None),  # untouched
        # sell limit: fills if high >= L at max(open, L)
        ("sell", "limit", 103.0, 100.0, 105.0, 95.0, 103.0),
        ("sell", "limit", 98.0, 100.0, 105.0, 95.0, 100.0),
        ("sell", "limit", 106.0, 100.0, 105.0, 95.0, None),
        # buy stop: triggers if high >= S, fills at max(open, S)
        ("buy", "stop", 103.0, 100.0, 105.0, 95.0, 103.0),
        ("buy", "stop", 99.0, 100.0, 105.0, 95.0, 100.0),  # gapped through: open
        ("buy", "stop", 106.0, 100.0, 105.0, 95.0, None),
        # sell stop: triggers if low <= S, fills at min(open, S)
        ("sell", "stop", 97.0, 100.0, 105.0, 95.0, 97.0),
        ("sell", "stop", 90.0, 85.0, 88.0, 80.0, 85.0),  # gapped stop fills at the open
        ("sell", "stop", 94.0, 100.0, 105.0, 95.0, None),
    ],
)
def test_limit_and_stop_rules(side, kind, level, open_, high, low, price):
    d = _model().decide(
        _order(side=side, qty=10.0, kind=kind, limit=level),
        _quote(open=open_, high=high, low=low),
    )
    if price is None:
        assert d.quantity == 0.0 and d.carry == 0.0  # DAY order expires
    else:
        assert d.quantity == 10.0
        assert d.price == pytest.approx(price)


@dataclass(frozen=True)
class _StopOrder(Order):
    stop_price: float | None = None


def test_stop_limit_triggers_then_checks_the_limit():
    ok = _StopOrder("c", "X.US", "buy", 10.0, "stop_limit", limit_price=104.0, stop_price=103.0)
    assert _model().decide(ok, _quote()).price == pytest.approx(103.0)
    too_high = _StopOrder(
        "c", "X.US", "buy", 10.0, "stop_limit", limit_price=104.0, stop_price=103.0
    )
    gapped = _model().decide(too_high, _quote(open=106.0, high=107.0, low=105.5))
    assert gapped.quantity == 0.0 and gapped.reason == "limit_not_reached"


def test_stop_limit_without_stop_price_never_fills():
    d = _model().decide(_order(kind="stop_limit", limit=100.0, qty=1.0), _quote())
    assert d.quantity == 0.0 and d.reason == "missing_stop_price"


def test_missing_high_low_uses_the_open():
    d = _model().decide(
        _order(kind="limit", limit=99.0, qty=1.0), _quote(high=None, low=None, open=100.0)
    )
    assert d.quantity == 0.0


def test_limits_can_be_ignored():
    d = _model(honour_limits=False).decide(_order(kind="limit", limit=1.0, qty=1.0), _quote())
    assert (d.quantity, d.price) == (1.0, 100.0)


def test_participation_applies_to_limit_orders_too():
    d = _model().decide(_order(kind="limit", limit=97.0, qty=2_000.0), _quote(volume=10_000.0))
    assert d.quantity == pytest.approx(1_000.0) and d.price == 97.0
    assert d.carry == pytest.approx(1_000.0)


# ---- gap guard ------------------------------------------------------------------------


def test_gap_guard_expires_orders_after_a_long_gap():
    d = _model().decide(_order(), _quote(gap_days=30.0))
    assert d.quantity == 0.0 and d.carry == 0.0 and d.reason == "gap"
    assert _model().decide(_order(), _quote(gap_days=3.0)).quantity == 100.0
    assert _model(max_gap_days=None).decide(_order(), _quote(gap_days=300.0)).quantity == 100.0


# ---- lagged market statistics ---------------------------------------------------------


def _bars(n=40, seed=1) -> pd.DataFrame:
    rng = np.random.default_rng(seed)
    rows = []
    for ticker in ("A.US", "B.US"):
        close = 100 * np.exp(np.cumsum(rng.normal(0, 0.02, n)))
        for i in range(n):
            rows.append(
                {
                    "ticker": ticker,
                    "timestamp": pd.Timestamp("2025-01-01", tz="UTC") + pd.Timedelta(days=i),
                    "high": close[i] * 1.01,
                    "low": close[i] * 0.99,
                    "close": close[i],
                    "adj_close": close[i],
                    "volume": float(1_000 + 10 * i),
                }
            )
    return pd.DataFrame(rows)


def test_market_stats_are_lagged_one_bar_per_ticker():
    bars = _bars()
    spec = MarketStatsSpec(adv_window=5, vol_window=5, spread_window=5)
    stats = lagged_market_stats(bars, spec)
    a = bars[bars.ticker == "A.US"]
    sa = stats.loc[a.index]
    # adv at row i = median volume of rows i-5..i-1
    assert np.isnan(sa["adv"].iloc[4])
    assert sa["adv"].iloc[5] == pytest.approx(np.median(a["volume"].iloc[0:5]))
    returns = np.log(a["close"]).diff()
    assert sa["sigma_daily"].iloc[6] == pytest.approx(returns.iloc[1:6].std(ddof=1))
    assert np.isnan(sa["sigma_daily"].iloc[5])
    assert sa["half_spread_bps"].isna().all()  # no estimator asked for


def test_market_stats_do_not_see_the_fill_bar():
    bars = _bars()
    spec = MarketStatsSpec(
        adv_window=5, vol_window=5, spread_window=5, spread_estimator="corwin_schultz"
    )
    base = lagged_market_stats(bars, spec)
    changed = bars.copy()
    row = changed[changed.ticker == "A.US"].index[20]
    changed.loc[row, ["high", "low", "close", "adj_close", "volume"]] = [
        500.0,
        1.0,
        400.0,
        400.0,
        1e9,
    ]
    after = lagged_market_stats(changed, spec)
    assert after.loc[:row].equals(base.loc[:row])  # the fill bar and before are unchanged
    assert not after.loc[row + 1].equals(base.loc[row + 1])


def test_market_stats_spread_estimate_is_positive_and_in_row_order():
    bars = _bars().sample(frac=1.0, random_state=0)  # shuffled input
    spec = MarketStatsSpec(
        adv_window=5, vol_window=5, spread_window=5, spread_estimator="abdi_ranaldo"
    )
    stats = lagged_market_stats(bars, spec)
    assert list(stats.index) == list(bars.index)
    assert (stats["half_spread_bps"].dropna() >= 0).all()
    assert stats["half_spread_bps"].notna().sum() > 0


def test_market_stats_rescale_for_splits():
    bars = _bars(seed=2)
    a = bars.ticker == "A.US"
    split = bars.loc[a].index[20]
    adjusted = bars.copy()
    # raw prices halve before the split; adj_close carries the continuous series
    before = a & (bars.index < split)
    adjusted.loc[before, ["high", "low", "close"]] *= 2.0
    spec = MarketStatsSpec(
        adv_window=5, vol_window=5, spread_window=5, spread_estimator="corwin_schultz"
    )
    pd.testing.assert_frame_equal(
        lagged_market_stats(adjusted, spec)[["sigma_daily", "half_spread_bps"]],
        lagged_market_stats(bars, spec)[["sigma_daily", "half_spread_bps"]],
    )


def test_spec_lookback_and_merge():
    spec = MarketStatsSpec(adv_window=20, vol_window=20)
    assert spec.lookback_bars == 21
    merged = spec.merge(MarketStatsSpec(spread_window=30, spread_estimator="abdi_ranaldo"))
    assert merged.spread_estimator == "abdi_ranaldo"
    assert merged.lookback_bars == 31
    assert spec.merge(None) is spec


def test_empty_bars_give_empty_stats():
    empty = _bars().iloc[:0]
    assert lagged_market_stats(empty, MarketStatsSpec()).empty


def test_gap_guard_allows_two_bars_of_the_bar_interval():
    """RS-14: a monthly bar is ~31 days after its decision; that is no gap."""
    assert _model().decide(_order(), _quote(gap_days=31.0, bar_days=30.0)).quantity == 100.0
    assert _model().decide(_order(), _quote(gap_days=14.0, bar_days=7.0)).quantity == 100.0
    assert _model().decide(_order(), _quote(gap_days=55.0, bar_days=30.0)).reason is None
    assert _model().decide(_order(), _quote(gap_days=90.0, bar_days=30.0)).reason == "gap"
    # daily bars keep the calendar limit
    assert _model().decide(_order(), _quote(gap_days=8.0, bar_days=1.0)).reason == "gap"

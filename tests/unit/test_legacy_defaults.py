"""BL-43: legacy defaults fixed, old param sets still load."""

from __future__ import annotations

import json
from datetime import date, datetime

import numpy as np
import pandas as pd
import pytest

from stonks.core.types import Portfolio
from stonks.features.indicators import efficiency_ratio, kama
from stonks.lab.catalog import strategy_catalog
from stonks.store.lake import DuckDBLake
from stonks.strategies.base import strategy_metadata
from stonks.strategies.examples.donchian_breakout import DonchianBreakout
from stonks.strategies.examples.momentum import Momentum
from stonks.strategies.examples.trendline_breakout import TrendlineBreakoutStrategy
from stonks.strategies.rule_based import RuleStrategy
from stonks.strategies.rules import RuleSpecError, validate_spec
from stonks.strategies.rules.sample import SampleLake

# ---- momentum 12-1 -------------------------------------------------------------------


def test_momentum_defaults_to_six_month_lookback_with_a_skip_month():
    p = Momentum({}).params
    assert p["lookback_days"] == 126
    assert p["skip_days"] == 21
    spec = {s.name: s for s in Momentum.parameter_spec()}
    assert spec["skip_days"].bounds == (0, 63)


def test_momentum_old_param_sets_keep_their_meaning():
    # a saved pre-BL-43 param set pins lookback_days and has no skip_days
    old = {"lookback_days": 20, "threshold": 0.0, "allocation": 1.0}
    assert Momentum(old).params["skip_days"] == 0
    assert Momentum({"lookback_days": 5}).params["skip_days"] == 0
    assert Momentum({"lookback_days": 126, "skip_days": 21}).params["skip_days"] == 21


def test_momentum_old_saved_params_load(tmp_path):
    (tmp_path / "params.json").write_text(
        json.dumps({"lookback_days": 20, "threshold": 0.0, "allocation": 1.0})
    )
    loaded = Momentum.load(tmp_path)
    assert loaded.params["lookback_days"] == 20
    assert loaded.params["skip_days"] == 0


@pytest.fixture
def lake(tmp_path):
    db = DuckDBLake(tmp_path / "lake.duckdb")
    db.migrate()
    dates = pd.bdate_range("2025-01-01", periods=200)
    # up for 160 bars, then down: the skip month hides the recent fall
    closes = np.concatenate([100 * (1.01 ** np.arange(160)), np.zeros(40)])
    closes[160:] = closes[159] * (0.98 ** np.arange(1, 41))
    db.upsert_prices(
        pd.DataFrame(
            {
                "ticker": "A.US",
                "date": [d.date() for d in dates],
                "open": closes,
                "high": closes,
                "low": closes,
                "close": closes,
                "adj_close": closes,
                "volume": 1_000.0,
            }
        )
    )
    yield db, dates, closes
    db.close()


def test_momentum_skip_days_ends_the_window_a_month_back(lake):
    db, dates, closes = lake
    i = 179
    as_of = dates[i].date()
    s = Momentum({"lookback_days": 60, "skip_days": 21, "threshold": -1.0})
    expected = closes[i - 21] / closes[i - 21 - 60] - 1.0
    assert s.estimate_return("A.US", as_of, db) == pytest.approx(expected)
    plain = Momentum({"lookback_days": 60, "skip_days": 0, "threshold": -1.0})
    assert plain.estimate_return("A.US", as_of, db) == pytest.approx(
        closes[i] / closes[i - 60] - 1.0
    )


def test_momentum_needs_lookback_plus_skip_bars(lake):
    db, dates, _ = lake
    s = Momentum({"lookback_days": 60, "skip_days": 21, "threshold": -1.0})
    assert s.estimate_return("A.US", dates[80].date(), db) is None
    assert s.estimate_return("A.US", dates[81].date(), db) is not None


# ---- breakouts off single stocks -----------------------------------------------------


@pytest.mark.parametrize("cls", [DonchianBreakout, TrendlineBreakoutStrategy])
def test_breakouts_default_off_equities_with_opt_in(cls):
    assert cls.applicable_asset_classes == ("commodity", "crypto", "bond")
    s = cls({})
    assert s.params["ticker"] == "BTC-USD.CC"
    assert "equity" not in s.applicable_asset_classes
    opted = cls({"ticker": "AAPL.US", "asset_classes": ["equity"]})
    assert opted.applicable_asset_classes == ("equity",)
    assert "Grimes" in (cls.__doc__ or "") + (cls.hypothesis or "")


@pytest.mark.parametrize("cls", [DonchianBreakout, TrendlineBreakoutStrategy])
def test_breakout_old_param_sets_still_load(cls, tmp_path):
    old = dict(cls({}).params)
    old["ticker"] = "AAPL.US"
    (tmp_path / "params.json").write_text(json.dumps(old))
    assert cls.load(tmp_path).params["ticker"] == "AAPL.US"


# ---- ER and KAMA ---------------------------------------------------------------------


def test_efficiency_ratio_is_one_on_a_straight_line_and_low_on_chop():
    line = pd.Series(np.arange(30, dtype=float))
    assert efficiency_ratio(line, 10).iloc[-1] == pytest.approx(1.0)
    assert np.isnan(efficiency_ratio(line, 10).iloc[9])
    chop = pd.Series([1.0, 2.0] * 15)
    assert efficiency_ratio(chop, 10).iloc[-1] == pytest.approx(0.0)


def test_kama_follows_a_trend_and_sits_still_in_chop():
    line = pd.Series(np.arange(100, dtype=float))
    k = kama(line, 10, 2, 30)
    # ER = 1: smoothing at the fast constant (2/3)^2, so a steady lag of
    # (1 - sc) / sc = 1.25 bars
    assert line.iloc[-1] - k.iloc[-1] == pytest.approx(1.25, abs=0.01)
    chop = pd.Series([10.0, 11.0] * 50)
    kc = kama(chop, 10, 2, 30)
    assert abs(kc.iloc[-1] - kc.iloc[-10]) < 0.1
    with pytest.raises(ValueError):
        kama(line, 10, 30, 2)


def _cmp(left, op, right):
    def operand(v):
        if isinstance(v, str):
            return {"type": "indicator", "id": v}
        return {"type": "constant", "value": v}

    return {"type": "compare", "left": operand(left), "op": op, "right": operand(right)}


def _spec(risk=None, indicators=None):
    return {
        "version": 1,
        "indicators": indicators
        or [{"id": "close", "kind": "close"}, {"id": "er", "kind": "efficiency_ratio"}],
        "entry": _cmp("close", ">", 0),
        "rank": {"by": "close"},
        "sizing": {"max_positions": 1},
        "risk": risk or {},
    }


def test_spec_accepts_er_and_kama_indicators():
    spec = validate_spec(
        _spec(
            indicators=[
                {"id": "close", "kind": "close"},
                {"id": "er", "kind": "efficiency_ratio", "period": 10},
                {"id": "k", "kind": "kama", "period": 10, "fast": 2, "slow": 30},
            ]
        )
    )
    assert [i.kind for i in spec.indicators] == ["close", "efficiency_ratio", "kama"]
    with pytest.raises(RuleSpecError):
        validate_spec(_spec(indicators=[{"id": "k", "kind": "kama", "fast": 30, "slow": 2}]))


def test_spec_bounds_on_the_trailing_stops():
    validate_spec(_spec({"trailing_stop_vol_multiple": 0.5, "trailing_stop_atr_multiple": 3}))
    for bad in (
        {"trailing_stop_vol_multiple": 0.1},
        {"trailing_stop_vol_multiple": 3.5},
        {"trailing_stop_atr_multiple": 0.5},
        {"trailing_stop_atr_multiple": 7},
    ):
        with pytest.raises(RuleSpecError):
            validate_spec(_spec(bad))


def test_old_specs_without_trailing_stops_are_unchanged():
    spec = validate_spec(_spec())
    assert spec.risk.trailing_stop_vol_multiple is None
    assert spec.risk.trailing_stop_atr_multiple is None


def _frame(closes) -> pd.DataFrame:
    closes = np.asarray(closes, dtype=float)
    return pd.DataFrame(
        {
            "timestamp": pd.bdate_range("2026-01-01", periods=len(closes)),
            "open": closes,
            "high": closes,
            "low": closes,
            "close": closes,
            "adj_close": closes,
            "volume": np.full(len(closes), 1000.0),
        }
    )


def _walk(strategy: RuleStrategy, frame: pd.DataFrame, start: int) -> list[tuple[int, str]]:
    """Run estimate + decide bar by bar; returns (bar, side) of each order."""
    lake = SampleLake({"A": frame})
    port = Portfolio(cash=1000.0)
    trades = []
    for i in range(start, len(frame)):
        ts = frame["timestamp"].iloc[i].to_pydatetime()
        price = float(frame["close"].iloc[i])
        est = strategy.estimate_return("A", ts, lake)
        picks = [(est, "A")] if est is not None else []
        for o in strategy.decide(picks, port, {"A": price}, ts):
            trades.append((i, o.side))
            if o.side == "buy":
                port = Portfolio(cash=port.cash - o.quantity * price, positions={"A": o.quantity})
            else:
                port = Portfolio(cash=port.cash + o.quantity * price, positions={})
        if trades and trades[-1][1] == "sell":
            break
    return trades


def test_atr_trailing_stop_exits_below_the_high_water_mark():
    # rises by 1 a bar to 29 (bar 19), then 27.5 (no exit: stop at 27), then 26.5
    closes = list(np.arange(10.0, 30.0)) + [27.5, 26.5, 26.0]
    s = RuleStrategy(
        {"spec": _spec({"trailing_stop_atr_multiple": 2.0, "trailing_stop_period": 5})}
    )
    trades = _walk(s, _frame(closes), start=10)
    assert trades == [(10, "buy"), (21, "sell")]


def test_vol_trailing_stop_exits_on_a_drop_and_no_stop_holds():
    rng = np.random.default_rng(3)
    closes = list(100 * np.exp(np.cumsum(0.002 + rng.normal(0, 0.005, 60)))) + [0.0] * 3
    closes[60] = closes[59] * 0.85
    closes[61] = closes[60]
    closes[62] = closes[60]
    stop = RuleStrategy(
        {"spec": _spec({"trailing_stop_vol_multiple": 0.5, "trailing_stop_period": 20})}
    )
    trades = _walk(stop, _frame(closes), start=30)
    assert trades[0] == (30, "buy")
    assert trades[-1] == (60, "sell")
    plain = RuleStrategy({"spec": _spec()})
    assert _walk(plain, _frame(closes), start=30) == [(30, "buy")]


# ---- metadata backfill ---------------------------------------------------------------


def test_every_catalogued_strategy_has_a_hypothesis_and_family():
    missing = [
        sid for sid, cls in strategy_catalog().items() if not strategy_metadata(cls).hypothesis
    ]
    assert missing == []
    no_family = [
        sid
        for sid, cls in strategy_catalog().items()
        if strategy_metadata(cls).alpha_family == "other" and not _is_wrapper_or_benchmark(sid)
    ]
    assert no_family == []


def test_every_trading_example_has_a_label_horizon():
    zero = [
        sid
        for sid, cls in strategy_catalog().items()
        if strategy_metadata(cls).label_horizon_bars == 0 and not _is_wrapper_or_benchmark(sid)
    ]
    assert zero == []


def _is_wrapper_or_benchmark(sid: str) -> bool:
    from stonks.lab.catalog import is_wrapper

    return is_wrapper(strategy_catalog()[sid]) or sid == "buy_and_hold"


def test_date_and_datetime_as_of_agree_for_momentum(lake):
    db, dates, _ = lake
    s = Momentum({"lookback_days": 60, "skip_days": 21, "threshold": -1.0})
    d = dates[150]
    assert s.estimate_return("A.US", d.date(), db) == s.estimate_return(
        "A.US", datetime.combine(d.date(), datetime.min.time()), db
    )
    assert isinstance(d.date(), date)

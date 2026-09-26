"""TVLDeviationStrategy: TVL alignment (causality), the indicator, the
long/flat hysteresis rule, staleness, persistence."""

from __future__ import annotations

from datetime import date, datetime, timedelta

import numpy as np
import pandas as pd
import pytest

from stonks.core.interval import Interval
from stonks.core.types import Portfolio
from stonks.store.lake import DuckDBLake
from stonks.strategies.examples.tvl_deviation import (
    TVLDeviationStrategy,
    align_tvl,
    tvl_deviation_states,
)

T = "ETH-USD.CC"
START = date(2026, 1, 1)
PARAMS = {"ticker": T, "fit_length": 5, "atr_lookback": 10, "threshold": 0.25}


def _days(n: int) -> list[date]:
    return [START + timedelta(days=i) for i in range(n)]


def _daily_bars(closes: np.ndarray, spread: float = 0.02) -> pd.DataFrame:
    closes = np.asarray(closes, dtype=float)
    opens = np.concatenate([[closes[0]], closes[:-1]])
    return pd.DataFrame(
        {
            "ticker": T,
            "timestamp": [datetime.combine(d, datetime.min.time()) for d in _days(len(closes))],
            "open": opens,
            "high": np.maximum(opens, closes) * (1 + spread / 2),
            "low": np.minimum(opens, closes) * (1 - spread / 2),
            "close": closes,
            "adj_close": closes,
            "volume": 1000.0,
        }
    )


def _tvl_frame(days: list[date], tvl, chain: str = "ethereum") -> pd.DataFrame:
    return pd.DataFrame(
        {
            "chain": chain,
            "observation_date": days,
            "tvl_usd": np.asarray(tvl, dtype=float),
            "source": "test",
        }
    )


def _lake(tmp_path, closes, tvl_days, tvl) -> DuckDBLake:
    lake = DuckDBLake(tmp_path / "lake.duckdb")
    lake.migrate()
    lake.upsert_bars(_daily_bars(closes), interval=Interval.DAY_1)
    if len(tvl_days):
        lake.upsert_defi_tvl(_tvl_frame(tvl_days, tvl))
    return lake


def _at(i: int) -> datetime:
    return datetime.combine(START + timedelta(days=i), datetime.min.time())


def _proportional(n: int, seed: int = 3) -> tuple[np.ndarray, np.ndarray]:
    """TVL random walk and a close that tracks TVL stamped the previous day
    exactly (close_d = TVL_{d-1} / 1e7), so the fitted price equals the
    close until we perturb it."""
    rng = np.random.default_rng(seed)
    tvl = 1e10 * np.exp(np.cumsum(rng.normal(0, 0.03, n)))
    close = np.empty(n)
    close[0] = tvl[0] / 1e7
    close[1:] = tvl[:-1] / 1e7
    return tvl, close


# ---- spec -------------------------------------------------------------------


def test_spec():
    specs = {s.name: s for s in TVLDeviationStrategy.parameter_spec()}
    assert (specs["fit_length"].default, specs["fit_length"].bounds) == (7, (5, 60))
    assert (specs["atr_lookback"].default, specs["atr_lookback"].bounds) == (30, (10, 90))
    assert (specs["threshold"].default, specs["threshold"].bounds) == (0.25, (0.0, 1.0))
    assert specs["chain"].default == "ethereum"
    assert specs["chain"].tunable is False
    assert specs["ticker"].default == "ETH-USD.CC"
    assert specs["interval"].default == "1d"
    assert specs["max_tvl_age_days"].default == 3
    assert TVLDeviationStrategy.applicable_asset_classes == ("crypto",)
    assert TVLDeviationStrategy.id == "tvl_deviation"


# ---- alignment --------------------------------------------------------------


def test_align_uses_only_tvl_stamped_before_the_bar_day():
    obs = [date(2026, 1, 1), date(2026, 1, 2), date(2026, 1, 3)]
    tvl = [10.0, 20.0, 30.0]
    bars = [date(2026, 1, 1), date(2026, 1, 2), date(2026, 1, 3), date(2026, 1, 4)]
    aligned, age = align_tvl(bars, obs, tvl)
    # bar d sees the value stamped d-1 at the latest; the first bar sees nothing
    assert np.isnan(aligned[0])
    assert aligned[1:].tolist() == [10.0, 20.0, 30.0]
    assert age[1:].tolist() == [1, 1, 1]


def test_align_carries_forward_and_reports_age():
    obs = [date(2026, 1, 1)]
    bars = [date(2026, 1, 2), date(2026, 1, 5), date(2026, 1, 6)]
    aligned, age = align_tvl(bars, obs, [7.0])
    assert aligned.tolist() == [7.0, 7.0, 7.0]
    assert age.tolist() == [1, 4, 5]


def test_align_with_no_tvl_is_all_nan():
    aligned, age = align_tvl([date(2026, 1, 2)], [], [])
    assert np.isnan(aligned).all()
    assert (age < 0).all()


# ---- rule -------------------------------------------------------------------


def test_hysteresis_rule():
    ind = np.array([0.1, -0.1, -0.3, -0.1, 0.0, 0.05, -0.26, np.nan, 0.2])
    # long below -0.25, held until the indicator goes above 0; NaN holds
    assert tvl_deviation_states(ind, 0.25).tolist() == [0, 0, 1, 1, 1, 0, 1, 1, 0]


def test_zero_threshold_goes_long_on_any_negative_value():
    assert tvl_deviation_states(np.array([0.1, -0.01, 0.0, 0.01]), 0.0).tolist() == [0, 1, 1, 0]


# ---- strategy on a lake -----------------------------------------------------


def test_causal_tvl_stamped_on_the_bar_day_is_invisible(tmp_path):
    n = 40
    tvl, close = _proportional(n)
    days = _days(n)
    lake = _lake(tmp_path, close, days, tvl)
    try:
        i = n - 5
        before = TVLDeviationStrategy(PARAMS).extract_features(T, _at(i), lake).values
        assert before["tvl"] == pytest.approx(tvl[i - 1])
        # Rewrite the TVL stamped on bar day i and later: nothing at bar i moves.
        future = _tvl_frame(days[i:], tvl[i:] * 5.0)
        lake.upsert_defi_tvl(future)
        after = TVLDeviationStrategy(PARAMS).extract_features(T, _at(i), lake).values
        assert after == before
        # Rewriting the value stamped the day before *does* move bar i.
        lake.upsert_defi_tvl(_tvl_frame([days[i - 1]], [tvl[i - 1] * 5.0]))
        moved = TVLDeviationStrategy(PARAMS).extract_features(T, _at(i), lake).values
        assert moved["tvl"] != before["tvl"]
        assert moved["ind"] != before["ind"]
    finally:
        lake.close()


def test_price_on_the_tvl_line_gives_zero_indicator(tmp_path):
    tvl, close = _proportional(40)
    lake = _lake(tmp_path, close, _days(40), tvl)
    try:
        state = TVLDeviationStrategy(PARAMS).extract_features(T, _at(39), lake).values
        assert state["pred"] == pytest.approx(close[39], rel=1e-9)
        assert state["ind"] == pytest.approx(0.0, abs=1e-6)
    finally:
        lake.close()


def test_price_below_tvl_line_goes_long_and_above_goes_flat(tmp_path):
    n = 40
    tvl, close = _proportional(n)
    dip = close.copy()
    dip[-1] *= 0.85  # a sharp drop the TVL did not follow
    lake = _lake(tmp_path, dip, _days(n), tvl)
    try:
        s = TVLDeviationStrategy(PARAMS)
        state = s.extract_features(T, _at(n - 1), lake).values
        assert state["ind"] < -0.25
        assert state["signal"] == 1.0
        est = s.estimate_return(T, _at(n - 1), lake)
        assert est is not None and est > 0
    finally:
        lake.close()

    pop = close.copy()
    pop[-1] *= 1.15
    path = tmp_path / "b"
    path.mkdir()
    lake = _lake(path, pop, _days(n), tvl)
    try:
        s = TVLDeviationStrategy(PARAMS)
        state = s.extract_features(T, _at(n - 1), lake).values
        assert state["ind"] > 0
        assert state["signal"] == 0.0
        assert s.estimate_return(T, _at(n - 1), lake) is None
    finally:
        lake.close()


def test_stale_tvl_gives_no_estimate(tmp_path):
    n = 40
    tvl, close = _proportional(n)
    close[-1] *= 0.85
    days = _days(n)
    # TVL stops 5 days before the last bar: last usable value is 5 days old.
    lake = _lake(tmp_path, close, days[: n - 5], tvl[: n - 5])
    try:
        s = TVLDeviationStrategy(PARAMS)
        assert s.estimate_return(T, _at(n - 1), lake) is None
        assert s.extract_features(T, _at(n - 1), lake).values == {}
        # three days old is still fine
        assert s.extract_features(T, _at(n - 3), lake).values["tvl_age_days"] == 3
    finally:
        lake.close()


def test_missing_tvl_gives_no_estimate(tmp_path):
    n = 40
    _, close = _proportional(n)
    lake = _lake(tmp_path, close, [], [])
    try:
        assert TVLDeviationStrategy(PARAMS).estimate_return(T, _at(n - 1), lake) is None
    finally:
        lake.close()


def test_other_chain_is_not_used(tmp_path):
    n = 40
    tvl, close = _proportional(n)
    lake = _lake(tmp_path, close, _days(n), tvl)
    try:
        s = TVLDeviationStrategy({**PARAMS, "chain": "solana"})
        assert s.estimate_return(T, _at(n - 1), lake) is None
    finally:
        lake.close()


def test_lake_without_tvl_surface_is_tolerated():
    class _BarsOnly:
        def get_bars(self, ticker, interval, start, end):
            return _daily_bars(np.linspace(100, 120, 40))

    assert TVLDeviationStrategy(PARAMS).estimate_return(T, _at(39), _BarsOnly()) is None


def test_not_enough_history_gives_no_estimate(tmp_path):
    tvl, close = _proportional(40)
    lake = _lake(tmp_path, close, _days(40), tvl)
    try:
        assert TVLDeviationStrategy(PARAMS).estimate_return(T, _at(8), lake) is None
    finally:
        lake.close()


def test_other_ticker_gives_no_estimate(tmp_path):
    tvl, close = _proportional(40)
    lake = _lake(tmp_path, close, _days(40), tvl)
    try:
        assert TVLDeviationStrategy(PARAMS).estimate_return("BTC-USD.CC", _at(39), lake) is None
    finally:
        lake.close()


# ---- decide / persistence ---------------------------------------------------


def test_decide_buys_when_picked_and_sells_when_not():
    s = TVLDeviationStrategy(PARAMS)
    buy = s.decide([(0.5, T)], Portfolio(cash=1000.0, positions={}), {T: 100.0}, _at(1))
    assert [(o.side, o.quantity) for o in buy] == [("buy", 10.0)]
    sell = s.decide([], Portfolio(cash=0.0, positions={T: 3.0}), {T: 100.0}, _at(1))
    assert [(o.side, o.quantity) for o in sell] == [("sell", 3.0)]


def test_save_load_round_trip(tmp_path):
    s = TVLDeviationStrategy({**PARAMS, "chain": "arbitrum"})
    s.save(tmp_path / "a")
    loaded = TVLDeviationStrategy.load(tmp_path / "a")
    assert loaded.params == s.params


def test_registry_round_trip(tmp_path):
    from stonks.registry.store import StrategyRegistry
    from stonks.store.state import SqliteState

    state = SqliteState(tmp_path / "state.sqlite")
    state.migrate()
    try:
        reg = StrategyRegistry(state=state, artifacts_dir=tmp_path / "artifacts")
        s = TVLDeviationStrategy(PARAMS)
        sid = reg.register(s, reports=[])
        loaded = reg.load(sid)
        assert type(loaded) is TVLDeviationStrategy
        assert loaded.params == s.params
    finally:
        state.close()


def test_catalogued():
    from stonks.lab.catalog import strategy_catalog

    assert strategy_catalog()["tvl_deviation"] is TVLDeviationStrategy

"""Bar accessors return split/dividend-adjusted history by default."""

from __future__ import annotations

from datetime import datetime

import numpy as np
import pandas as pd
import pytest

from stonks.core.interval import Interval
from stonks.store.lake import DuckDBLake
from stonks.strategies._common import BarCache, get_last_n_bars
from stonks.strategies.examples.donchian_breakout import DonchianBreakout
from stonks.strategies.examples.momentum import Momentum

DAYS = pd.bdate_range("2024-05-01", periods=40)
SPLIT_DAY = DAYS[30].date()  # 10:1 split effective on bar 30
DIV_DAY = DAYS[10].date()


def _closes() -> np.ndarray:
    # gently rising, then /10 at the split
    closes = np.linspace(1000.0, 1039.0, len(DAYS))
    closes[30:] /= 10.0
    return closes


@pytest.fixture
def lake(tmp_path):
    lake = DuckDBLake(tmp_path / "lake.duckdb")
    lake.migrate()
    closes = _closes()
    lake.upsert_prices(
        pd.DataFrame(
            {
                "ticker": "X.US",
                "date": [d.date() for d in DAYS],
                "open": closes,
                "high": closes,
                "low": closes,
                "close": closes,
                "adj_close": closes,  # vendor adj_close not refreshed
                "volume": 1_000.0,
            }
        )
    )
    lake.upsert_stock_splits(pd.DataFrame([{"ticker": "X.US", "date": SPLIT_DAY, "ratio": 10.0}]))
    yield lake
    lake.close()


def _as_of(i: int) -> datetime:
    d = DAYS[i]
    return datetime(d.year, d.month, d.day, 23, 59)


def test_last_n_closes_is_adjusted_by_default_and_raw_on_request(lake):
    cache = BarCache(lake)
    adjusted = cache.last_n_closes("X.US", Interval.DAY_1, _as_of(39), 40)
    raw = cache.last_n_closes("X.US", Interval.DAY_1, _as_of(39), 40, basis="raw")
    assert raw.tolist() == pytest.approx(_closes().tolist())
    assert adjusted.tolist() == pytest.approx((np.linspace(1000.0, 1039.0, 40) / 10).tolist())
    assert np.all(np.diff(adjusted) > 0)  # no crash


def test_last_n_bars_adjusts_ohlc_and_volume(lake):
    bars = BarCache(lake).last_n_bars("X.US", Interval.DAY_1, _as_of(39), 40)
    assert bars["open"].iloc[0] == pytest.approx(100.0)
    assert bars["close"].iloc[29] == pytest.approx(102.9)
    assert bars["volume"].iloc[0] == pytest.approx(10_000.0)
    assert bars["volume"].iloc[39] == pytest.approx(1_000.0)


def test_adjusted_history_is_causal(lake):
    cache = BarCache(lake)
    # as of the bar before the ex-date nothing is adjusted
    before = cache.last_n_closes("X.US", Interval.DAY_1, _as_of(29), 30)
    assert before.tolist() == pytest.approx(_closes()[:30].tolist())
    bars = cache.last_n_bars("X.US", Interval.DAY_1, _as_of(29), 30)
    assert bars["close"].tolist() == pytest.approx(_closes()[:30].tolist())
    between = cache.bars_between("X.US", Interval.DAY_1, DAYS[0], _as_of(29))
    assert between["close"].tolist() == pytest.approx(_closes()[:30].tolist())


def test_last_close_is_the_raw_quote(lake):
    ts, close = BarCache(lake).last_close("X.US", Interval.DAY_1, _as_of(39))
    assert close == pytest.approx(_closes()[39])


def test_bars_between_adjusts_as_of_window_end(lake):
    out = BarCache(lake).bars_between("X.US", Interval.DAY_1, DAYS[25], _as_of(35))
    assert out["close"].iloc[0] == pytest.approx(_closes()[25] / 10)
    assert out["close"].iloc[-1] == pytest.approx(_closes()[35])


def test_dividend_adjusts_history(lake):
    lake.upsert_dividends(
        pd.DataFrame(
            [
                {
                    "ticker": "X.US",
                    "ex_date": DIV_DAY,
                    "amount": 9.0,
                    "currency": None,
                    "pay_date": None,
                    "record_date": None,
                    "declaration_date": None,
                }
            ]
        )
    )
    closes = BarCache(lake).last_n_closes("X.US", Interval.DAY_1, _as_of(20), 21)
    factor = 1 - 9.0 / _closes()[9]
    assert closes[0] == pytest.approx(_closes()[0] * factor)
    assert closes[10:].tolist() == pytest.approx(_closes()[10:21].tolist())


def test_get_last_n_bars_adjusts_by_default(lake):
    out = get_last_n_bars(lake, "X.US", Interval.DAY_1, _as_of(39), 20)
    raw = get_last_n_bars(lake, "X.US", Interval.DAY_1, _as_of(39), 20, basis="raw")
    assert out["close"].iloc[0] == pytest.approx(_closes()[20] / 10)
    assert raw["close"].iloc[0] == pytest.approx(_closes()[20])
    before = get_last_n_bars(lake, "X.US", Interval.DAY_1, _as_of(29), 10)
    assert before["close"].tolist() == pytest.approx(_closes()[20:30].tolist())


def test_lake_without_corporate_actions_reader_gets_raw_history(lake):
    class BarsOnly:
        def get_bars(self, *args, **kwargs):
            return lake.get_bars(*args, **kwargs)

    closes = BarCache(BarsOnly()).last_n_closes("X.US", Interval.DAY_1, _as_of(39), 40)
    assert closes.tolist() == pytest.approx(_closes().tolist())


def test_split_is_not_a_momentum_crash(lake):
    strat = Momentum({"lookback_days": 20, "threshold": 0.0, "allocation": 1.0})
    r = strat.estimate_return("X.US", DAYS[39].date(), lake)
    assert r == pytest.approx(1039.0 / 1019.0 - 1.0)


def test_split_is_not_a_donchian_breakdown(lake):
    strat = DonchianBreakout(
        {"lookback": 10, "ticker": "X.US", "allocation": 1.0, "interval": "1d"}
    )
    features = strat.extract_features("X.US", _as_of(30), lake)
    assert features.get("signal") == 1.0  # still in the up-trend, not a -1 breakdown
    assert strat.estimate_return("X.US", _as_of(30), lake) is not None

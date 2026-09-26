"""``train_bars`` (the ML strategies' fit-window fetch) reads split-safe,
look-ahead-safe prices through the shared ``BarCache``."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date

import numpy as np
import pandas as pd
import pytest

from stonks.core.interval import Interval
from stonks.store.lake import DuckDBLake
from stonks.strategies._common import LakeBarCaches
from stonks.strategies.examples._nt888_common import train_bars

FAR_PAST, FAR_FUTURE = pd.Timestamp("1900-01-01"), pd.Timestamp("2200-01-01")


@dataclass
class _Dataset:
    lake: DuckDBLake
    train_window: tuple[date, date]


@pytest.fixture
def lake(tmp_path):
    dates = pd.bdate_range("2025-01-02", periods=40)
    close = np.linspace(100.0, 120.0, 40)
    lake = DuckDBLake(tmp_path / "lake.duckdb")
    lake.migrate()
    rows = []
    for ticker, split_at in (("SPL.US", 20), ("LATE.US", 35), ("PLAIN.US", None)):
        raw = close.copy()
        if split_at is not None:
            raw[split_at:] /= 2.0
        rows.append(
            pd.DataFrame(
                {
                    "ticker": ticker,
                    "date": [d.date() for d in dates],
                    "open": raw,
                    "high": raw * 1.01,
                    "low": raw * 0.99,
                    "close": raw,
                    "adj_close": raw,
                    "volume": 1000.0,
                }
            )
        )
    lake.upsert_prices(pd.concat(rows, ignore_index=True))
    lake.upsert_stock_splits(
        pd.DataFrame(
            [
                {"ticker": "SPL.US", "date": dates[20].date(), "ratio": 2.0},
                {"ticker": "LATE.US", "date": dates[35].date(), "ratio": 2.0},
            ]
        )
    )
    yield lake, dates
    lake.close()


def test_split_inside_the_train_window_is_back_adjusted(lake):
    lake, dates = lake
    ds = _Dataset(lake, (dates[0].date(), dates[29].date()))
    bars = train_bars(ds, "SPL.US", Interval.DAY_1)
    assert len(bars) == 30
    # no fake 50% crash: the whole window is in post-split shares
    assert np.max(np.abs(np.diff(np.log(bars["close"].to_numpy())))) < 0.05
    assert bars["close"].iloc[-1] == pytest.approx(
        lake.get_bars("SPL.US", Interval.DAY_1, FAR_PAST, FAR_FUTURE)["close"].iloc[29]
    )


def test_split_after_train_end_does_not_leak(lake):
    lake, dates = lake
    ds = _Dataset(lake, (dates[0].date(), dates[29].date()))
    raw = lake.get_bars("LATE.US", Interval.DAY_1, FAR_PAST, FAR_FUTURE).iloc[:30]
    bars = train_bars(ds, "LATE.US", Interval.DAY_1)
    np.testing.assert_allclose(bars["close"].to_numpy(), raw["close"].to_numpy())


def test_ticker_without_corporate_actions_reads_like_the_lake(lake):
    lake, dates = lake
    ds = _Dataset(lake, (dates[0].date(), dates[29].date()))
    raw = lake.get_bars("PLAIN.US", Interval.DAY_1, dates[0], dates[29]).reset_index(drop=True)
    pd.testing.assert_frame_equal(train_bars(ds, "PLAIN.US", Interval.DAY_1), raw)


def test_uses_the_given_cache(lake):
    lake, dates = lake
    ds = _Dataset(lake, (dates[0].date(), dates[29].date()))
    caches = LakeBarCaches()
    train_bars(ds, "PLAIN.US", Interval.DAY_1, caches=caches)
    assert len(caches) == 1


def test_empty_window_raises(lake):
    lake, _ = lake
    ds = _Dataset(lake, (date(2020, 1, 1), date(2020, 2, 1)))
    with pytest.raises(ValueError, match="no bars"):
        train_bars(ds, "PLAIN.US", Interval.DAY_1)

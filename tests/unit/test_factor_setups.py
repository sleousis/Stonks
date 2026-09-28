"""Named chart setups as factors and screener metrics (roadmap 23.13):
candlestick patterns, NR7, the inside bar, a volatility contraction pattern
and the squeeze, each checked on hand-made bars."""

from __future__ import annotations

from datetime import date

import numpy as np
import pandas as pd
import pytest

from stonks.factors.engine import evaluate, prepare_bars
from stonks.factors.registry import factor_sets, get_factor
from stonks.screener.data import ScreenData
from stonks.screener.registry import metric_for, metric_ids
from stonks.store.lake import DuckDBLake

Row = tuple[float, float, float, float]


def _bars(rows: list[Row], ticker: str = "A") -> pd.DataFrame:
    dates = pd.bdate_range("2024-01-01", periods=len(rows))
    return pd.DataFrame(
        [
            {
                "ticker": ticker,
                "timestamp": d,
                "open": o,
                "high": h,
                "low": low,
                "close": c,
                "adj_close": c,
                "volume": 1000.0,
            }
            for d, (o, h, low, c) in zip(dates, rows, strict=True)
        ]
    )


def _last(fid: str, rows: list[Row]) -> float:
    panel = evaluate(get_factor(fid).node, prepare_bars(_bars(rows)), ["A"])
    return float(panel["A"].iloc[-1])


def _trend(n: int, start: float = 100.0, step: float = 1.0) -> list[Row]:
    """``n`` bars drifting by ``step`` a bar with a 2-point range."""
    out = []
    for i in range(n):
        c = start + step * i
        out.append((c - step / 2, c + 1.0, c - 1.0, c))
    return out


def test_the_setups_set_is_registered():
    ids = factor_sets()["setups"]
    for fid in ("engulfing", "hammer", "star", "inside_bar", "nr7", "vcp", "squeeze"):
        assert fid in ids
        f = get_factor(fid)
        assert f.family == "setup"
        assert f.hypothesis
        assert f.asset_classes == ("equity", "crypto", "commodity", "bond")


def test_engulfing_is_signed():
    base = _trend(12, step=-1.0)
    bull = [*base, (90.0, 90.5, 88.0, 88.5), (88.0, 92.0, 87.5, 91.5)]
    bear = [*base, (88.0, 90.5, 87.5, 90.0), (90.5, 91.0, 87.0, 87.5)]
    none = [*base, (90.0, 90.5, 88.0, 88.5), (88.8, 89.0, 88.0, 88.9)]
    assert _last("engulfing", bull) == 1.0
    assert _last("engulfing", bear) == -1.0
    assert _last("engulfing", none) == 0.0


def test_hammer_after_a_decline_and_shooting_star_after_a_rise():
    down = _trend(15, start=120.0, step=-1.0)
    hammer = [*down, (105.0, 105.3, 100.0, 105.2)]
    assert _last("hammer", hammer) == 1.0
    up = _trend(15, start=100.0, step=1.0)
    star = [*up, (115.0, 120.0, 114.9, 115.2)]
    assert _last("hammer", star) == -1.0
    # the same hammer shape after a rise is not a hammer
    assert _last("hammer", [*up, (115.0, 115.3, 110.0, 115.2)]) == 0.0


def test_morning_and_evening_star():
    down = _trend(12, start=120.0, step=-1.0)
    morning = [
        *down,
        (110.0, 110.5, 104.5, 105.0),
        (104.5, 105.0, 103.5, 104.2),
        (104.5, 109.5, 104.0, 109.0),
    ]
    assert _last("star", morning) == 1.0
    up = _trend(12, start=100.0, step=1.0)
    evening = [
        *up,
        (111.0, 116.5, 110.5, 116.0),
        (116.5, 117.5, 116.0, 116.8),
        (116.5, 117.0, 111.0, 112.0),
    ]
    assert _last("star", evening) == -1.0


def test_inside_bar_and_nr7_follow_the_trend():
    up = _trend(60, step=0.5)
    last = up[-1][3]
    inside = [*up, (last, last + 0.5, last - 0.5, last + 0.2)]
    assert _last("inside_bar", inside) == 1.0
    assert _last("nr7", inside) == 1.0  # a 1-point range, the narrowest of seven
    wide = [*up, (last, last + 3.0, last - 3.0, last + 0.2)]
    assert _last("inside_bar", wide) == 0.0
    assert _last("nr7", wide) == 0.0
    down = _trend(60, start=200.0, step=-0.5)
    last = down[-1][3]
    assert _last("nr7", [*down, (last, last + 0.4, last - 0.4, last - 0.1)]) == -1.0


def _contraction(tight: float) -> list[Row]:
    """A long rise, then three bases whose ranges shrink to ``tight``."""
    rows = _trend(260, start=50.0, step=0.25)
    top = rows[-1][3]
    for width, n in ((6.0, 20), (3.0, 10), (tight, 10)):
        for i in range(n):
            c = top - width / 4 + (width / 4 if i % 2 else -width / 4)
            rows.append((c, c + width / 2, c - width / 2, c))
    return rows


def test_vcp_needs_successively_tighter_ranges_in_an_uptrend():
    assert _last("vcp", _contraction(1.0)) == 1.0
    assert _last("vcp", _contraction(5.0)) == 0.0
    falling = _trend(300, start=200.0, step=-0.25)
    assert _last("vcp", falling) == 0.0


def test_squeeze_fires_when_bands_sit_inside_the_channel():
    rng = np.random.default_rng(1)
    calm: list[Row] = []
    c = 100.0
    for _ in range(40):
        c += 0.02
        calm.append((c, c + 2.0 + rng.uniform(0, 0.1), c - 2.0, c))  # wide bars, flat closes
    assert _last("squeeze", calm) == 1.0
    trending = _trend(40, step=2.0)
    assert _last("squeeze", trending) == 0.0


def test_every_setup_is_a_screener_metric():
    for fid in factor_sets()["setups"]:
        assert f"setup_{fid}" in metric_ids()
        metric = metric_for(f"setup_{fid}")
        assert metric.group == "price"
        assert metric.unit == "ratio"


def test_screener_metric_reads_the_lake_point_in_time(tmp_path):
    lake = DuckDBLake(tmp_path / "lake.duckdb")
    lake.migrate()
    up = _trend(60, step=0.5)
    last = up[-1][3]
    rows = [*up, (last, last + 0.5, last - 0.5, last + 0.2), (last, last + 5, last - 5, last)]
    frame = _bars(rows, "A.US").rename(columns={"timestamp": "date"})
    frame["date"] = [d.date() for d in frame["date"]]
    lake.upsert_prices(frame)
    day = frame["date"].iloc[-2]
    got = metric_for("setup_nr7").compute(ScreenData(lake, ["A.US", "B.US"], day))
    assert got == {"A.US": pytest.approx(1.0)}
    # the wide bar the next day is not seen on the NR7 day
    later = metric_for("setup_nr7").compute(ScreenData(lake, ["A.US"], frame["date"].iloc[-1]))
    assert later == {"A.US": pytest.approx(0.0)}
    assert isinstance(day, date)
    lake.close()

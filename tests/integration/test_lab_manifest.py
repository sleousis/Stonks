"""data_fingerprint over a real DuckDB lake (BL-06)."""

from __future__ import annotations

from datetime import date

import pandas as pd

from stonks.lab.dataset import LabDataset
from stonks.lab.manifest import build_manifest, data_fingerprint

UNIVERSE = ["UP.US", "DOWN.US", "FLAT.US"]


def _ds(lake, universe=UNIVERSE, start=date(2025, 10, 1), end=date(2026, 4, 1)):
    return LabDataset(lake=lake, universe=list(universe), start=start, end=end)


def test_fingerprint_describes_each_ticker(lake_trending):
    fp = data_fingerprint(_ds(lake_trending))
    assert set(fp["tickers"]) == set(UNIVERSE)
    up = fp["tickers"]["UP.US"]
    n = len(pd.bdate_range("2025-10-01", "2026-04-01"))
    assert up["count"] == n
    assert up["first"].startswith("2025-10-01")
    assert up["last"].startswith("2026-04-01")
    assert len(up["bars_hash"]) == 32
    assert fp["interval"] == "1d"
    assert fp["window"] == ["2025-10-01", "2026-04-01"]
    assert len(fp["hash"]) == 64


def test_fingerprint_is_stable(lake_trending):
    assert data_fingerprint(_ds(lake_trending)) == data_fingerprint(_ds(lake_trending))


def test_fingerprint_changes_when_one_close_changes(lake_trending):
    before = data_fingerprint(_ds(lake_trending))
    lake_trending.con.execute(
        "UPDATE bars SET close = close + 0.01 WHERE ticker = 'FLAT.US' "
        "AND timestamp = TIMESTAMP '2026-01-15'"
    )
    after = data_fingerprint(_ds(lake_trending))
    assert after["hash"] != before["hash"]
    assert after["tickers"]["FLAT.US"]["bars_hash"] != before["tickers"]["FLAT.US"]["bars_hash"]
    assert after["tickers"]["UP.US"] == before["tickers"]["UP.US"]


def test_fingerprint_ignores_bars_outside_the_window(lake_trending):
    ds = _ds(lake_trending, start=date(2025, 11, 1), end=date(2026, 1, 1))
    before = data_fingerprint(ds)
    lake_trending.con.execute(
        "UPDATE bars SET close = close + 1 WHERE timestamp > TIMESTAMP '2026-02-01'"
    )
    assert data_fingerprint(ds) == before


def test_ticker_without_bars_has_zero_count(lake_trending):
    fp = data_fingerprint(_ds(lake_trending, universe=["UP.US", "NONE.US"]))
    assert fp["tickers"]["NONE.US"] == {
        "count": 0,
        "first": None,
        "last": None,
        "bars_hash": None,
    }


def test_build_manifest_carries_the_fingerprint(lake_trending):
    ds = _ds(lake_trending)
    assert build_manifest(None, ds, {})["data_fingerprint"] == data_fingerprint(ds)


# ---- RS-18: every input that changes a result changes the hash ----------------------


def test_fingerprint_changes_when_one_open_changes(lake_trending):
    before = data_fingerprint(_ds(lake_trending))
    lake_trending.con.execute(
        "UPDATE bars SET open = open + 0.01 WHERE ticker = 'UP.US' "
        "AND timestamp = TIMESTAMP '2026-01-15'"
    )
    assert data_fingerprint(_ds(lake_trending))["hash"] != before["hash"]


def test_fingerprint_changes_when_high_low_or_volume_change(lake_trending):
    before = data_fingerprint(_ds(lake_trending))["hash"]
    for col, delta in (("high", 0.01), ("low", -0.01), ("volume", 1)):
        lake_trending.con.execute(
            f"UPDATE bars SET {col} = {col} + {delta} WHERE ticker = 'DOWN.US' "
            "AND timestamp = TIMESTAMP '2026-01-15'"
        )
        after = data_fingerprint(_ds(lake_trending))["hash"]
        assert after != before, col
        before = after


def test_fingerprint_changes_when_a_split_or_dividend_is_added(lake_trending):
    before = data_fingerprint(_ds(lake_trending))["hash"]
    lake_trending.con.execute(
        "INSERT INTO stock_splits (ticker, date, ratio) VALUES ('UP.US', DATE '2025-12-01', 4.0)"
    )
    with_split = data_fingerprint(_ds(lake_trending))["hash"]
    assert with_split != before
    lake_trending.con.execute(
        "INSERT INTO dividends (ticker, ex_date, amount) VALUES ('UP.US', DATE '2025-12-05', 0.5)"
    )
    assert data_fingerprint(_ds(lake_trending))["hash"] != with_split


def test_fingerprint_covers_warm_up_bars_before_the_start(lake_trending):
    ds = _ds(lake_trending, start=date(2025, 12, 1), end=date(2026, 3, 1))
    before = data_fingerprint(ds)["hash"]
    lake_trending.con.execute(
        "UPDATE bars SET close = close + 1 WHERE ticker = 'UP.US' "
        "AND timestamp = TIMESTAMP '2025-11-03'"
    )
    assert data_fingerprint(ds)["hash"] != before


# ---- reference tickers, the benchmark and every interval read (BE-28) -----------------


def _bump(lake, ticker: str, interval: str = "1d") -> None:
    lake.con.execute(
        f"UPDATE bars SET close = close + 0.01 WHERE ticker = '{ticker}' "
        f"AND interval = '{interval}' AND timestamp = TIMESTAMP '2026-01-15'"
    )


def test_a_reference_ticker_bar_changes_the_hash(lake_trending):
    ds = LabDataset(
        lake=lake_trending, universe=["UP.US"], start=date(2025, 10, 1), end=date(2026, 4, 1),
        reference_tickers=("DOWN.US",), benchmark="none",
    )  # fmt: skip
    before = data_fingerprint(ds)
    assert "DOWN.US" in before["references"]
    _bump(lake_trending, "DOWN.US")
    assert data_fingerprint(ds)["hash"] != before["hash"]


def test_a_benchmark_bar_changes_the_hash(lake_trending):
    ds = LabDataset(
        lake=lake_trending, universe=["UP.US"], start=date(2025, 10, 1), end=date(2026, 4, 1),
        benchmark="FLAT.US",
    )  # fmt: skip
    before = data_fingerprint(ds)
    _bump(lake_trending, "FLAT.US")
    assert data_fingerprint(ds)["hash"] != before["hash"]


def test_an_intraday_run_also_hashes_the_daily_bars_it_reads(lake_trending):
    from datetime import datetime, timedelta

    from stonks.core.interval import Interval

    hourly = pd.DataFrame(
        [
            {"ticker": "UP.US", "timestamp": datetime(2026, 1, 15, 14) + timedelta(hours=h),
             "open": 1.0, "high": 1.0, "low": 1.0, "close": 1.0, "adj_close": 1.0, "volume": 1}
            for h in range(3)
        ]
    )  # fmt: skip
    lake_trending.upsert_bars(hourly, interval=Interval.HOUR_1)
    ds = LabDataset(
        lake=lake_trending, universe=["UP.US"], start=date(2026, 1, 1), end=date(2026, 2, 1),
        interval=Interval.HOUR_1, benchmark="none",
    )  # fmt: skip
    before = data_fingerprint(ds)
    _bump(lake_trending, "UP.US", "1d")
    assert data_fingerprint(ds)["hash"] != before["hash"]

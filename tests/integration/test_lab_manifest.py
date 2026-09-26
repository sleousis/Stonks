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
    assert len(up["close_hash"]) == 32
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
    assert after["tickers"]["FLAT.US"]["close_hash"] != before["tickers"]["FLAT.US"]["close_hash"]
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
        "close_hash": None,
    }


def test_build_manifest_carries_the_fingerprint(lake_trending):
    ds = _ds(lake_trending)
    assert build_manifest(None, ds, {})["data_fingerprint"] == data_fingerprint(ds)

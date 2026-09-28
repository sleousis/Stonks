"""The factor panel cache (roadmap 22.2): hits, misses, invalidation when
the data changes, and no caching through a point-in-time view."""

from __future__ import annotations

from datetime import date

import pandas as pd
import pytest

from stonks.factors.base import ExpressionFactor
from stonks.factors.cache import (
    MemoryPanelCache,
    PanelKey,
    ParquetPanelCache,
    data_fingerprint,
    slot_of,
)
from stonks.factors.engine import PanelRequest
from stonks.factors.panels import FactorEngine
from stonks.store.lake import DuckDBLake
from stonks.store.pit import PointInTimeLake

PANEL = pd.DataFrame(
    {"A": [1.0, 2.0], "B": [3.0, float("nan")]},
    index=pd.DatetimeIndex(["2024-01-01", "2024-01-02"], name="timestamp"),
)


@pytest.mark.parametrize("make", [lambda p: MemoryPanelCache(), lambda p: ParquetPanelCache(p)])
def test_hit_miss_and_invalidation(tmp_path, make):
    cache = make(tmp_path / "cache")
    key = PanelKey("slot1", "fp1")
    assert cache.get(key) is None
    cache.put(key, PANEL)
    pd.testing.assert_frame_equal(cache.get(key), PANEL, check_freq=False)
    assert (cache.hits, cache.misses) == (1, 1)
    # the data changed: the old panel is dropped
    assert cache.get(PanelKey("slot1", "fp2")) is None
    assert cache.get(key) is None
    assert len(cache) == 0


def test_parquet_cache_keeps_one_file_per_slot(tmp_path):
    cache = ParquetPanelCache(tmp_path)
    cache.put(PanelKey("s", "a"), PANEL)
    cache.put(PanelKey("s", "b"), PANEL * 2)
    cache.put(PanelKey("t", "a"), PANEL)
    assert sorted(p.name for p in tmp_path.glob("*.parquet")) == ["s.b.parquet", "t.a.parquet"]
    assert cache.get(PanelKey("s", "b"))["A"].tolist() == [2.0, 4.0]


def test_parquet_cache_treats_a_torn_file_as_a_miss(tmp_path):
    cache = ParquetPanelCache(tmp_path)
    (tmp_path / "s.a.parquet").write_bytes(b"not parquet")
    assert cache.get(PanelKey("s", "a")) is None
    assert not (tmp_path / "s.a.parquet").exists()
    assert ParquetPanelCache(tmp_path / "missing").get(PanelKey("s", "a")) is None


def test_memory_cache_evicts_the_least_recently_used():
    cache = MemoryPanelCache(max_panels=2)
    for slot in ("a", "b"):
        cache.put(PanelKey(slot, "f"), PANEL)
    assert cache.get(PanelKey("a", "f")) is not None  # a is now the newest
    cache.put(PanelKey("c", "f"), PANEL)
    assert cache.get(PanelKey("b", "f")) is None
    assert cache.get(PanelKey("a", "f")) is not None


def test_slots_differ_by_every_part():
    base = slot_of("t", ["A"], (date(2024, 1, 1), date(2024, 2, 1)), "1d")
    assert base == slot_of("t", ["A"], (date(2024, 1, 1), date(2024, 2, 1)), "1d")
    assert base != slot_of("u", ["A"], (date(2024, 1, 1), date(2024, 2, 1)), "1d")
    assert base != slot_of("t", ["B"], (date(2024, 1, 1), date(2024, 2, 1)), "1d")
    assert base != slot_of("t", ["A"], (date(2024, 1, 2), date(2024, 2, 1)), "1d")
    assert base != slot_of("t", ["A"], (date(2024, 1, 1), date(2024, 2, 1)), "1h")
    assert base != slot_of("t", ["A"], (date(2024, 1, 1), date(2024, 2, 1)), "1d", {"x": 1})


# ---- the engine over a lake ----------------------------------------------------------------


def _prices(lake, closes: dict[str, list[float]], start="2024-01-01"):
    rows = []
    days = pd.bdate_range(start, periods=max(len(v) for v in closes.values()))
    for ticker, values in closes.items():
        for d, c in zip(days, values, strict=False):
            rows.append(
                {
                    "ticker": ticker,
                    "date": d.date(),
                    "open": c,
                    "high": c,
                    "low": c,
                    "close": c,
                    "adj_close": c,
                    "volume": 1000,
                }
            )
    lake.upsert_prices(pd.DataFrame(rows))


@pytest.fixture
def lake(tmp_path):
    lake = DuckDBLake(tmp_path / "lake.duckdb")
    lake.migrate()
    _prices(lake, {"A": [1, 2, 3, 4, 5, 6], "B": [6, 5, 4, 3, 2, 1]})
    yield lake
    lake.close()


REQUEST = PanelRequest(("A", "B"), date(2024, 1, 1), date(2024, 1, 8))
FACTOR = ExpressionFactor("ma2", "Mean($close, 2)")


def test_engine_caches_and_invalidates_when_bars_change(lake, tmp_path):
    engine = FactorEngine(lake, ParquetPanelCache(tmp_path / "panels"))
    first = engine.panel(FACTOR, REQUEST)
    again = engine.panel(FACTOR, REQUEST)
    pd.testing.assert_frame_equal(first, again, check_freq=False)
    assert (engine.cache.hits, engine.cache.misses) == (1, 1)
    # a corrected bar changes the fingerprint: the next read recomputes
    _prices(lake, {"A": [1, 2, 3, 4, 5, 100]})
    fresh = engine.panel(FACTOR, REQUEST)
    assert engine.cache.misses == 2
    assert fresh.loc["2024-01-08", "A"] == pytest.approx((5 + 100) / 2)


def test_engine_slices_a_cached_panel_to_sampled_dates(lake):
    engine = FactorEngine(lake, MemoryPanelCache())
    dates = pd.DatetimeIndex(["2024-01-03", "2024-01-05"])
    sampled = engine.panel(FACTOR, REQUEST, dates)
    assert list(sampled.index) == list(dates)
    engine.panel(FACTOR, REQUEST)
    assert engine.cache.hits == 1  # the whole-window panel was cached once


def test_no_cache_through_a_point_in_time_view(lake):
    view = PointInTimeLake(lake, pd.Timestamp("2024-01-05").to_pydatetime())
    assert data_fingerprint(view, ["A"], "1d", "2024-01-01", "2024-01-08") is None
    engine = FactorEngine(view, MemoryPanelCache())
    panel = engine.panel(FACTOR, REQUEST)
    assert panel.index[-1] == pd.Timestamp("2024-01-05")
    assert len(engine.cache) == 0


def test_fingerprint_covers_actions_tables_and_membership(lake):
    base = data_fingerprint(lake, ["A"], "1d", "2024-01-01", "2024-01-08")
    spans = pd.DataFrame([{"ticker": "A", "start_date": date(2024, 1, 1), "end_date": None}])
    with_spans = data_fingerprint(lake, ["A"], "1d", "2024-01-01", "2024-01-08", membership=spans)
    assert base and with_spans and base != with_spans
    with_table = data_fingerprint(
        lake, ["A"], "1d", "2024-01-01", "2024-01-08", tables=("income_statement",)
    )
    assert with_table and with_table != base
    lake.upsert_dividends(
        pd.DataFrame(
            [
                {
                    "ticker": "A",
                    "ex_date": date(2024, 1, 4),
                    "amount": 0.1,
                    "currency": "USD",
                    "pay_date": None,
                    "record_date": None,
                    "declaration_date": None,
                }
            ]
        )
    )
    assert data_fingerprint(lake, ["A"], "1d", "2024-01-01", "2024-01-08") != base
    # a table that does not exist: no fingerprint, so no caching
    assert data_fingerprint(lake, ["A"], "1d", "2024-01-01", "2024-01-08", tables=("nope",)) is None

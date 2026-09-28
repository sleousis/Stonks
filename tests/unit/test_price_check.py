"""The second-source price check (roadmap 23.6): vendor closes and adjusted
returns against a second source, before the tick."""

from __future__ import annotations

from datetime import UTC, date, datetime

import pandas as pd
import pytest

from stonks.ingest.schemas import RawPriceBar
from stonks.production.halts import active_halts
from stonks.production.price_check import (
    BrokerMarks,
    DataSourceCloses,
    check_prices,
    latest_check,
    price_check_health,
    price_holds,
    record_check,
    run_price_check,
    tickers_to_check,
)
from stonks.production.price_check_settings import PriceCheckSettings

DAYS = pd.bdate_range("2026-08-17", "2026-09-25")
AS_OF = date(2026, 9, 25)
NOW = datetime(2026, 9, 25, 20, 40, tzinfo=UTC)


def closes(i: int) -> float:
    return 100.0 + i


def seed(lake, tickers=("A.US", "B.US", "C.US", "D.US")):
    rows = [
        {"ticker": t, "date": d.date(), "open": 1.0, "high": 1.0, "low": 1.0,
         "close": closes(i), "adj_close": closes(i), "volume": 10}
        for t in tickers
        for i, d in enumerate(DAYS)
    ]  # fmt: skip
    lake.upsert_prices(pd.DataFrame(rows))


class Second:
    """A second source: the same bars as the lake, or changed ones."""

    source_id = "second"

    def __init__(self, scale=None, split=None, missing=(), fail=()):
        self.scale = scale or {}
        self.split = split or {}
        self.missing = set(missing)
        self.fail = set(fail)
        self.asked: list[tuple[str, date | None, date | None]] = []

    def fetch_prices(self, ticker, since=None, until=None):
        self.asked.append((ticker, since, until))
        if ticker in self.fail:
            raise RuntimeError("vendor down")
        if ticker in self.missing:
            return []
        out = []
        for i, d in enumerate(DAYS):
            day = d.date()
            if (since and day < since) or (until and day > until):
                continue
            c = closes(i) * self.scale.get(ticker, 1.0)
            adj = c
            cut = self.split.get(ticker)
            if cut is not None and day < cut:
                adj = c / 2.0  # the second source knows of a 2:1 split
            out.append(RawPriceBar(ticker=ticker, date=day, open=c, high=c, low=c,
                                   close=c, adj_close=adj, volume=1))  # fmt: skip
        return out


def settings(**kw):
    return PriceCheckSettings(enabled=True, **kw)


def test_matching_sources_are_clean(lake):
    seed(lake)
    report = check_prices(lake, DataSourceCloses(Second()), ["A.US", "B.US"], AS_OF, settings())
    assert report.status == "clean" and report.held == ()
    assert report.compared == 2
    assert all(i.status == "ok" for i in report.items)


def test_a_large_close_gap_holds_that_ticker(lake):
    seed(lake)
    second = DataSourceCloses(Second(scale={"B.US": 1.10}))
    report = check_prices(lake, second, ["A.US", "B.US", "C.US", "D.US"], AS_OF, settings())
    assert report.status == "gaps" and report.held == ("B.US",)
    b = next(i for i in report.items if i.ticker == "B.US")
    assert b.close_gap == pytest.approx(0.1 / 1.1, rel=1e-6)
    assert "close" in b.detail


def test_a_missed_split_is_an_adjustment_gap(lake):
    seed(lake)
    second = DataSourceCloses(Second(split={"A.US": date(2026, 9, 10)}))
    report = check_prices(lake, second, ["A.US", "B.US", "C.US"], AS_OF, settings())
    a = next(i for i in report.items if i.ticker == "A.US")
    assert a.status == "gap" and a.adjustment_gap is not None and a.adjustment_gap > 0.4
    assert "adjust" in a.detail
    assert report.held == ("A.US",)


def test_most_tickers_gapping_is_systematic(lake):
    seed(lake)
    second = DataSourceCloses(Second(scale={"A.US": 1.2, "B.US": 1.2, "C.US": 1.2}))
    report = check_prices(lake, second, ["A.US", "B.US", "C.US", "D.US"], AS_OF, settings())
    assert report.status == "systematic"
    few = check_prices(lake, second, ["A.US", "D.US"], AS_OF, settings())
    assert few.status == "gaps"  # too few compared to call it systematic


def test_unknown_tickers_are_shown_not_held(lake):
    seed(lake)
    second = DataSourceCloses(Second(missing={"A.US"}, fail={"B.US"}))
    report = check_prices(lake, second, ["A.US", "B.US", "ZZZ.US"], AS_OF, settings())
    assert report.status == "unavailable" and report.held == ()
    status = {i.ticker: i.status for i in report.items}
    assert status == {"A.US": "unknown", "B.US": "unknown", "ZZZ.US": "unknown"}


def test_broker_marks_compare_the_close_in_major_units(lake):
    seed(lake, ("A.US", "VOD.LSE"))
    lake.con.execute(
        "INSERT INTO instruments (id, asset_class, currency) VALUES ('VOD.LSE', 'equity', 'GBX')"
    )

    class Quote:
        def __init__(self, ref):
            self.reference = ref

    class Broker:
        def quotes(self, tickers):
            last = closes(len(DAYS) - 1)
            return {"A.US": Quote(last * 1.05), "VOD.LSE": Quote(last / 100.0)}

    report = check_prices(lake, BrokerMarks(Broker()), ["A.US", "VOD.LSE"], AS_OF, settings())
    status = {i.ticker: i.status for i in report.items}
    assert status == {"A.US": "gap", "VOD.LSE": "ok"}


def test_tickers_to_check_are_held_then_signalled(state):
    state.execute(
        "INSERT INTO tick_runs (id, started_at, status) VALUES ('t1', '2026-09-24T21:00:00', 'ok')"
    )
    state.execute(
        "INSERT INTO portfolio_snapshots (tick_id, taken_at, cash, positions_json, total_value,"
        " as_of, portfolio_id) VALUES ('t1', '2026-09-24T21:00:00+00:00', 0, ?, 0,"
        " '2026-09-24', 'pf_default')",
        ['{"H.US": 3, "Z.US": 0}'],
    )
    state.execute(
        "INSERT INTO signals (as_of, strategy_id, ticker, tick_id, score) VALUES"
        " ('2026-09-24', 's1', 'S.US', 't1', 0.1), ('2026-09-24', 's1', 'H.US', 't1', 0.2),"
        " ('2026-09-01', 's1', 'OLD.US', 't0', 0.3)"
    )
    held, signalled = tickers_to_check(state, AS_OF)
    assert held == ["H.US"] and signalled == ["S.US"]


def test_a_run_is_stored_and_the_tick_reads_its_holds(state, lake):
    seed(lake)
    second = DataSourceCloses(Second(scale={"B.US": 1.10}))
    report = check_prices(lake, second, ["A.US", "B.US", "C.US", "D.US"], AS_OF, settings())
    record_check(state, report, checked_at=NOW)
    assert price_holds(state, AS_OF) == frozenset({"B.US"})
    assert price_holds(state, date(2026, 9, 28)) == frozenset()
    latest = latest_check(state)
    assert latest is not None and latest["status"] == "gaps"
    checks = {c.name: c for c in price_check_health(state)}
    assert checks["price_check"].ok and not checks["price_gap"].ok
    assert "B.US" in checks["price_gap"].detail


def test_a_systematic_gap_opens_the_operational_halt_until_a_clean_check(state, lake):
    seed(lake)
    bad = DataSourceCloses(Second(scale=dict.fromkeys(("A.US", "B.US", "C.US", "D.US"), 1.3)))
    report = run_price_check(
        state, lake, bad, ["A.US", "B.US", "C.US", "D.US"], AS_OF, settings(), now=NOW
    )
    assert report.status == "systematic"
    [halt] = active_halts(state, AS_OF)
    assert halt.kind == "operational" and "price check" in halt.reason
    health = {c.name: c for c in price_check_health(state)}
    assert not health["price_check"].ok  # health keeps the halt open meanwhile
    run_price_check(
        state, lake, DataSourceCloses(Second()), ["A.US", "B.US"], AS_OF, settings(), now=NOW
    )
    assert {c.name: c for c in price_check_health(state)}["price_check"].ok


def test_without_the_table_there_is_nothing_to_hold(tmp_path):
    from stonks.store.state import SqliteState

    bare = SqliteState(tmp_path / "bare.sqlite")
    try:
        assert price_holds(bare, AS_OF) == frozenset()
        assert price_check_health(bare) == []
    finally:
        bare.close()

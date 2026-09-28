"""load_prices / drop_stale_buys: stale closes mark and sell, never buy."""

from __future__ import annotations

from datetime import date

from stonks.core.types import Order
from stonks.production.prices import drop_stale_buys, held_tickers, load_prices


def test_fresh_universe_tickers_are_priced_and_buyable(lake_trending):
    # lake_trending's last bar is 2026-04-01 (a Wednesday).
    book = load_prices(
        lake_trending, ["UP.US", "FLAT.US"], [], date(2026, 4, 8), max_staleness_days=7
    )
    assert set(book.prices) == {"UP.US", "FLAT.US"}
    assert book.fresh == {"UP.US", "FLAT.US"}
    assert book.prices["FLAT.US"] == 50.0


def test_stale_universe_tickers_are_dropped(lake_trending):
    book = load_prices(
        lake_trending, ["UP.US", "FLAT.US"], [], date(2026, 4, 9), max_staleness_days=7
    )
    assert book.prices == {}
    assert book.fresh == frozenset()


def test_held_tickers_keep_their_last_close_however_old(lake_trending):
    book = load_prices(
        lake_trending, ["UP.US"], ["DOWN.US"], date(2026, 9, 1), max_staleness_days=7
    )
    assert book.prices == {"DOWN.US": 60.0}
    assert book.fresh == frozenset()


def test_held_ticker_outside_universe_is_priced(lake_trending):
    book = load_prices(
        lake_trending, ["UP.US"], ["DOWN.US"], date(2026, 4, 1), max_staleness_days=7
    )
    assert set(book.prices) == {"UP.US", "DOWN.US"}
    assert book.fresh == {"UP.US", "DOWN.US"}


def test_no_close_after_as_of_is_used(lake_trending):
    book = load_prices(lake_trending, ["UP.US"], [], date(2025, 10, 1), max_staleness_days=7)
    assert book.prices == {"UP.US": 100.0}


def test_volumes_come_from_the_same_bar_as_the_close(lake_trending):
    # The cost model's impact term needs the volume of the bar being priced.
    book = load_prices(
        lake_trending, ["UP.US"], ["DOWN.US"], date(2026, 4, 8), max_staleness_days=7
    )
    assert book.volumes == {"UP.US": 1_000_000.0, "DOWN.US": 1_000_000.0}


def test_unpriced_tickers_have_no_volume(lake_trending):
    book = load_prices(lake_trending, ["UP.US"], [], date(2026, 9, 1), max_staleness_days=7)
    assert book.volumes == {}


def test_drop_stale_buys_keeps_sells_and_fresh_buys():
    orders = [
        Order(client_id="1", ticker="A", side="buy", quantity=1),
        Order(client_id="2", ticker="B", side="buy", quantity=1),
        Order(client_id="3", ticker="B", side="sell", quantity=1),
    ]
    kept, dropped = drop_stale_buys(orders, {"A"})
    assert [o.client_id for o in kept] == ["1", "3"]
    assert dropped == ["B"]


def test_held_tickers_ignores_zero_positions():
    assert held_tickers({"A": 1.0, "B": 0.0, "C": -2.0}) == ["A", "C"]


def _london(lake) -> None:
    """VOD.LSE: FLAT.US's bars times 30, quoted in pence (GBX) like EODHD."""
    lake.con.execute(
        "INSERT INTO bars SELECT 'VOD.LSE', timestamp, interval, open * 30, high * 30,"
        " low * 30, close * 30, adj_close * 30, volume FROM bars WHERE ticker = 'FLAT.US'"
    )
    lake.con.execute(
        "INSERT INTO instruments (id, asset_class, currency) VALUES ('VOD.LSE', 'equity', 'GBX')"
    )


def test_pence_quoted_prices_come_out_in_pounds(lake_trending):
    _london(lake_trending)
    book = load_prices(
        lake_trending, ["VOD.LSE", "FLAT.US"], [], date(2026, 4, 8), max_staleness_days=7
    )
    assert book.prices == {"VOD.LSE": 15.0, "FLAT.US": 50.0}
    assert book.volumes["VOD.LSE"] == 1_000_000.0  # shares, not money


def test_history_comes_out_in_pounds_for_production_only(lake_trending):
    from stonks.production.prices import load_history

    _london(lake_trending)
    prod = load_history(lake_trending, ["VOD.LSE"], date(2026, 4, 1), bars=5)
    assert prod["VOD.LSE"]["close"].iloc[-1] == 15.0
    raw = load_history(lake_trending, ["VOD.LSE"], date(2026, 4, 1), bars=5, major_units=False)
    assert raw["VOD.LSE"]["close"].iloc[-1] == 1500.0


def test_protective_stop_atr_comes_out_in_pounds(lake_trending):
    from stonks.production.live.stops import load_atr

    _london(lake_trending)
    atr, closes = load_atr(lake_trending, ["VOD.LSE"], date(2026, 4, 1), 5)
    assert closes == {"VOD.LSE": 15.0}
    assert 0 < atr["VOD.LSE"] < 1.0  # (50.5 - 49.5) * 30 pence = 0.3 pounds

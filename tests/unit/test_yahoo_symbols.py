"""Canonical ticker <-> Yahoo Finance symbol mapping."""

from __future__ import annotations

import pytest

from stonks.ingest.sources.base import DataSourceError
from stonks.ingest.sources.yahoo import (
    YahooUnsupportedTickerError,
    from_yahoo_symbol,
    to_yahoo_symbol,
)


@pytest.mark.parametrize(
    ("canonical", "yahoo"),
    [
        ("AAPL.US", "AAPL"),
        ("BRK-B.US", "BRK-B"),
        ("BMW.XETRA", "BMW.DE"),
        ("VOD.LSE", "VOD.L"),
        ("MC.PA", "MC.PA"),
        ("ASML.AS", "ASML.AS"),
        ("NESN.SW", "NESN.SW"),
        ("SHOP.TO", "SHOP.TO"),
        ("BHP.AU", "BHP.AX"),
        ("0700.HK", "0700.HK"),
        ("RELIANCE.NSE", "RELIANCE.NS"),
        ("600519.SHG", "600519.SS"),
        ("BTC-USD.CC", "BTC-USD"),
        ("ETH-EUR.CC", "ETH-EUR"),
    ],
)
def test_round_trip(canonical, yahoo):
    assert to_yahoo_symbol(canonical) == yahoo
    asset_class = "crypto" if canonical.endswith(".CC") else "equity"
    assert from_yahoo_symbol(yahoo, asset_class=asset_class) == canonical


def test_exchange_suffix_is_case_insensitive():
    assert to_yahoo_symbol("bmw.xetra") == "bmw.DE"


@pytest.mark.parametrize("ticker", ["FOO.NOPE", "GC.COMM", "US10Y.GBOND", "AAPL"])
def test_unmapped_ticker_raises_clear_soft_fail_error(ticker):
    with pytest.raises(YahooUnsupportedTickerError) as info:
        to_yahoo_symbol(ticker)
    assert ticker in str(info.value)
    # Soft-fail family so a mixed universe keeps going past one bad ticker.
    assert isinstance(info.value, DataSourceError)


@pytest.mark.parametrize("symbol", ["BMW.XX", "GC=F", "^GSPC", ""])
def test_unknown_yahoo_symbol_raises(symbol):
    with pytest.raises(YahooUnsupportedTickerError):
        from_yahoo_symbol(symbol)


def test_crypto_reverse_mapping_rejects_exchange_suffix():
    with pytest.raises(YahooUnsupportedTickerError):
        from_yahoo_symbol("BTC-USD.L", asset_class="crypto")


def test_reverse_mapping_rejects_unsupported_asset_class():
    with pytest.raises(YahooUnsupportedTickerError):
        from_yahoo_symbol("GC", asset_class="commodity")

"""Ticker <-> Alpaca symbol mapping."""

from __future__ import annotations

import pytest

from stonks.execution.brokers.base import UnsupportedTickerError
from stonks.execution.brokers.symbols import from_alpaca_symbol, to_alpaca_symbol


@pytest.mark.parametrize(
    ("ticker", "symbol"),
    [("AAPL.US", "AAPL"), ("BRK-B.US", "BRK.B"), ("msft.us", "MSFT")],
)
def test_to_alpaca_symbol(ticker, symbol):
    assert to_alpaca_symbol(ticker) == symbol


@pytest.mark.parametrize(
    ("symbol", "ticker"),
    [("AAPL", "AAPL.US"), ("BRK.B", "BRK-B.US")],
)
def test_from_alpaca_symbol(symbol, ticker):
    assert from_alpaca_symbol(symbol) == ticker


@pytest.mark.parametrize("ticker", ["VOD.LSE", "BTC-USD.CC", "AAPL", "", ".US", "A B.US"])
def test_non_us_or_malformed_tickers_are_rejected(ticker):
    with pytest.raises(UnsupportedTickerError):
        to_alpaca_symbol(ticker)


@pytest.mark.parametrize("symbol", ["BTC/USD", "", "AAPL240119C00100000"])
def test_non_equity_alpaca_symbols_are_rejected(symbol):
    with pytest.raises(UnsupportedTickerError):
        from_alpaca_symbol(symbol)


def test_round_trip():
    for t in ["AAPL.US", "BRK-B.US", "BF-A.US"]:
        assert from_alpaca_symbol(to_alpaca_symbol(t)) == t

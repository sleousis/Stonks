"""Ticker <-> Alpaca symbol mapping."""

from __future__ import annotations

import pytest

from stonks.execution.brokers.base import UnsupportedTickerError
from stonks.execution.brokers.symbols import (
    from_alpaca_symbol,
    is_crypto_ticker,
    to_alpaca_symbol,
)


@pytest.mark.parametrize(
    ("ticker", "symbol"),
    [
        ("AAPL.US", "AAPL"),
        ("BRK-B.US", "BRK.B"),
        ("msft.us", "MSFT"),
        ("BTC-USD.CC", "BTC/USD"),
        ("eth-usdt.cc", "ETH/USDT"),
    ],
)
def test_to_alpaca_symbol(ticker, symbol):
    assert to_alpaca_symbol(ticker) == symbol


@pytest.mark.parametrize(
    ("symbol", "asset_class", "ticker"),
    [
        ("AAPL", None, "AAPL.US"),
        ("AAPL", "us_equity", "AAPL.US"),
        ("BRK.B", None, "BRK-B.US"),
        ("BTC/USD", None, "BTC-USD.CC"),
        ("BTC/USD", "crypto", "BTC-USD.CC"),
        # Alpaca reports crypto *positions* without the slash.
        ("BTCUSD", "crypto", "BTC-USD.CC"),
        ("ETHUSDT", "crypto", "ETH-USDT.CC"),
    ],
)
def test_from_alpaca_symbol(symbol, asset_class, ticker):
    assert from_alpaca_symbol(symbol, asset_class=asset_class) == ticker


@pytest.mark.parametrize("ticker", ["VOD.LSE", "AAPL", "", ".US", "A B.US", "BTC.CC", "GC.COMM"])
def test_unsupported_or_malformed_tickers_are_rejected(ticker):
    with pytest.raises(UnsupportedTickerError):
        to_alpaca_symbol(ticker)


@pytest.mark.parametrize(
    ("symbol", "asset_class"),
    [
        ("", None),
        ("AAPL240119C00100000", None),
        ("AAPL240119C00100000", "us_option"),
        ("XYZ", "crypto"),
    ],
)
def test_unsupported_alpaca_symbols_are_rejected(symbol, asset_class):
    with pytest.raises(UnsupportedTickerError):
        from_alpaca_symbol(symbol, asset_class=asset_class)


def test_round_trip():
    for t in ["AAPL.US", "BRK-B.US", "BF-A.US", "BTC-USD.CC", "SOL-USDC.CC"]:
        assert from_alpaca_symbol(to_alpaca_symbol(t)) == t


def test_is_crypto_ticker():
    assert is_crypto_ticker("BTC-USD.CC")
    assert not is_crypto_ticker("AAPL.US")

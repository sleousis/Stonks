"""Broker/aggregator symbols -> our canonical tickers (None = not covered)."""

from __future__ import annotations

import pytest

from stonks.execution.brokers.symbols import alpaca_symbol_to_ticker, to_canonical_ticker


@pytest.mark.parametrize(
    ("symbol", "kwargs", "ticker"),
    [
        ("AAPL", {"exchange": "NASDAQ"}, "AAPL.US"),
        ("aapl", {"exchange": "XNAS"}, "AAPL.US"),
        ("BRK.B", {"exchange": "NYSE"}, "BRK-B.US"),
        ("BRK/B", {"exchange": "NYSE"}, "BRK-B.US"),
        ("SPY", {"exchange": "ARCA", "asset_type": "etf"}, "SPY.US"),
        ("SPY", {"exchange": None, "currency": "USD"}, "SPY.US"),
        ("SHOP", {"exchange": "TSX"}, "SHOP.TO"),
        ("SHOP.TO", {"exchange": "XTSE"}, "SHOP.TO"),
        ("VOD", {"exchange": "LSE"}, "VOD.LSE"),
        ("SAP", {"exchange": "XETRA"}, "SAP.XETRA"),
        ("BTC", {"asset_type": "crypto", "currency": "USD"}, "BTC-USD.CC"),
        ("ETH/USDT", {"asset_type": "crypto"}, "ETH-USDT.CC"),
        ("BTC-USD", {"asset_type": "crypto"}, "BTC-USD.CC"),
    ],
)
def test_to_canonical_ticker_maps_known_symbols(symbol, kwargs, ticker):
    assert to_canonical_ticker(symbol, **kwargs) == ticker


@pytest.mark.parametrize(
    ("symbol", "kwargs"),
    [
        ("AAPL  260116C00200000", {"exchange": "NASDAQ", "asset_type": "option"}),
        ("AAPL", {"exchange": "XYZQ"}),  # unknown exchange
        ("SPY", {"exchange": None, "currency": "CAD"}),  # no exchange, not USD
        ("TOOLONGSYMBOL", {"exchange": "NYSE"}),
        ("", {"exchange": "NYSE"}),
        ("912828XX", {"exchange": None, "asset_type": "bond"}),
        ("BTC", {"asset_type": "crypto", "currency": None}),
    ],
)
def test_to_canonical_ticker_reports_unknown_as_none(symbol, kwargs):
    assert to_canonical_ticker(symbol, **kwargs) is None


def test_alpaca_symbol_to_ticker_is_the_non_raising_variant():
    assert alpaca_symbol_to_ticker("BRK.B", asset_class="us_equity") == "BRK-B.US"
    assert alpaca_symbol_to_ticker("BTCUSD", asset_class="crypto") == "BTC-USD.CC"
    assert alpaca_symbol_to_ticker("AAPL260116C00200000", asset_class="us_option") is None

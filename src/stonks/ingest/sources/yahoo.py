"""Yahoo Finance data source, wrapping the ``yfinance`` library.

``yfinance`` types (DataFrames with vendor column names, ``YFException``,
Yahoo symbols) stay inside this module: callers see canonical tickers
(``AAPL.US``), canonical row schemas, and :class:`DataSourceError`.

Ticker mapping
--------------
Canonical tickers use the EODHD-style ``<SYMBOL>.<EXCHANGE>`` form. Yahoo
uses a bare symbol for US listings, a vendor suffix for other exchanges
(``BMW.DE``), and a bare pair for crypto (``BTC-USD``). The closed map
:data:`_EXCHANGE_TO_YAHOO_SUFFIX` is the single source of truth; exchanges
missing from it raise :class:`YahooUnsupportedTickerError`. Commodity and
bond tickers are deliberately unmapped: Yahoo's futures (``GC=F``) and
yield indices (``^TNX``) are different instruments from the canonical
spot/benchmark series, so silently substituting them would corrupt bars.
"""

from __future__ import annotations

from stonks.core.types import AssetClass
from stonks.ingest.sources.base import DataSourceError


class YahooDataSourceError(DataSourceError):
    """Base for Yahoo adapter errors (soft-fail per ticker)."""


class YahooUnsupportedTickerError(YahooDataSourceError):
    """The canonical ticker (or Yahoo symbol) has no mapping."""


class YahooUnsupportedOperationError(YahooDataSourceError):
    """Yahoo does not offer reliable data for this operation."""


# Canonical exchange code -> Yahoo symbol suffix ("" = no suffix, US).
_EXCHANGE_TO_YAHOO_SUFFIX: dict[str, str] = {
    "US": "",
    "LSE": ".L",
    "XETRA": ".DE",
    "F": ".F",
    "PA": ".PA",
    "AS": ".AS",
    "BR": ".BR",
    "LS": ".LS",
    "MC": ".MC",
    "MI": ".MI",
    "SW": ".SW",
    "VI": ".VI",
    "CO": ".CO",
    "ST": ".ST",
    "OL": ".OL",
    "HE": ".HE",
    "IR": ".IR",
    "TO": ".TO",
    "V": ".V",
    "AU": ".AX",
    "HK": ".HK",
    "SHG": ".SS",
    "SHE": ".SZ",
    "KO": ".KS",
    "KQ": ".KQ",
    "NSE": ".NS",
    "BSE": ".BO",
    "SA": ".SA",
    "MX": ".MX",
    "JSE": ".JO",
    "TA": ".TA",
}
_YAHOO_SUFFIX_TO_EXCHANGE: dict[str, str] = {
    suffix: code for code, suffix in _EXCHANGE_TO_YAHOO_SUFFIX.items() if suffix
}
_CRYPTO_EXCHANGE = "CC"


def _split(ticker: str) -> tuple[str, str]:
    if "." not in ticker:
        raise YahooUnsupportedTickerError(
            f"{ticker!r} has no exchange suffix; expected canonical form like AAPL.US"
        )
    symbol, exchange = ticker.rsplit(".", 1)
    if not symbol:
        raise YahooUnsupportedTickerError(f"{ticker!r} has an empty symbol")
    return symbol, exchange.upper()


def to_yahoo_symbol(ticker: str) -> str:
    """Map a canonical ticker (``BMW.XETRA``) to its Yahoo symbol (``BMW.DE``)."""
    symbol, exchange = _split(ticker)
    if exchange == _CRYPTO_EXCHANGE:
        return symbol
    suffix = _EXCHANGE_TO_YAHOO_SUFFIX.get(exchange)
    if suffix is None:
        raise YahooUnsupportedTickerError(
            f"{ticker!r}: exchange {exchange!r} has no Yahoo Finance mapping "
            f"(supported: {sorted([*_EXCHANGE_TO_YAHOO_SUFFIX, _CRYPTO_EXCHANGE])})"
        )
    return f"{symbol}{suffix}"


def from_yahoo_symbol(symbol: str, asset_class: AssetClass = "equity") -> str:
    """Map a Yahoo symbol back to the canonical ticker.

    Yahoo's bare symbols are ambiguous (``BTC-USD`` vs a US stock), so the
    caller states the asset class; only ``equity`` and ``crypto`` map.
    """
    if not symbol or any(ch in symbol for ch in "=^"):
        raise YahooUnsupportedTickerError(f"Yahoo symbol {symbol!r} has no canonical mapping")
    if asset_class == "crypto":
        if "." in symbol:
            raise YahooUnsupportedTickerError(
                f"Yahoo crypto symbol {symbol!r} should be a bare pair like BTC-USD"
            )
        return f"{symbol}.{_CRYPTO_EXCHANGE}"
    if asset_class != "equity":
        raise YahooUnsupportedTickerError(
            f"Yahoo symbol {symbol!r}: asset class {asset_class!r} is not supported"
        )
    if "." not in symbol:
        return f"{symbol}.US"
    base, suffix = symbol.rsplit(".", 1)
    exchange = _YAHOO_SUFFIX_TO_EXCHANGE.get(f".{suffix.upper()}")
    if exchange is None or not base:
        raise YahooUnsupportedTickerError(
            f"Yahoo symbol {symbol!r}: suffix .{suffix} has no canonical exchange"
        )
    return f"{base}.{exchange}"

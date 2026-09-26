"""Map our tickers (``AAPL.US``, ``BRK-B.US``) to Alpaca symbols and back.

Only US equities are supported. Share classes use ``-`` in our tickers
(EODHD convention) and ``.`` at Alpaca (``BRK-B.US`` <-> ``BRK.B``).
"""

from __future__ import annotations

import re

from stonks.execution.brokers.base import UnsupportedTickerError

_US_SUFFIX = ".US"
_OUR_BASE = re.compile(r"^[A-Z]{1,6}(-[A-Z]{1,2})?$")
_ALPACA_EQUITY = re.compile(r"^[A-Z]{1,6}(\.[A-Z]{1,2})?$")


def to_alpaca_symbol(ticker: str) -> str:
    upper = ticker.strip().upper()
    if not upper.endswith(_US_SUFFIX):
        raise UnsupportedTickerError(
            f"ticker {ticker!r} is not a US listing (expected '<SYMBOL>.US'); "
            "the Alpaca broker only trades US equities"
        )
    base = upper[: -len(_US_SUFFIX)]
    if not _OUR_BASE.match(base):
        raise UnsupportedTickerError(f"ticker {ticker!r} is not a valid US equity ticker")
    return base.replace("-", ".")


def from_alpaca_symbol(symbol: str) -> str:
    upper = symbol.strip().upper()
    if not _ALPACA_EQUITY.match(upper):
        raise UnsupportedTickerError(
            f"Alpaca symbol {symbol!r} is not a US equity symbol; only US equities are supported"
        )
    return upper.replace(".", "-") + _US_SUFFIX

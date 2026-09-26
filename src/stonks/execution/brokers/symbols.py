"""Map our tickers to Alpaca symbols and back.

Supported:

- US equities: ``AAPL.US`` <-> ``AAPL``. Share classes use ``-`` in our
  tickers (EODHD convention) and ``.`` at Alpaca (``BRK-B.US`` <-> ``BRK.B``).
- Crypto pairs: ``BTC-USD.CC`` <-> ``BTC/USD``. Alpaca reports crypto
  *positions* without the slash (``BTCUSD``), so the reverse mapping takes
  the position's ``asset_class`` as a hint to split base and quote.

Everything else (non-US listings, commodities, bonds, options) raises
``UnsupportedTickerError``.
"""

from __future__ import annotations

import re

from stonks.execution.brokers.base import UnsupportedTickerError

_US_SUFFIX = ".US"
_CRYPTO_SUFFIX = ".CC"
_OUR_EQUITY_BASE = re.compile(r"^[A-Z]{1,6}(-[A-Z]{1,2})?$")
_ALPACA_EQUITY = re.compile(r"^[A-Z]{1,6}(\.[A-Z]{1,2})?$")
_CRYPTO_LEG = re.compile(r"^[A-Z0-9]{2,10}$")
# Quote currencies Alpaca lists crypto pairs against, longest first so a
# slash-less position symbol splits unambiguously (ETHUSDT -> ETH/USDT).
_CRYPTO_QUOTES = ("USDT", "USDC", "USD", "BTC")
_CRYPTO_CLASSES = frozenset({"crypto"})
_EQUITY_CLASSES = frozenset({"us_equity"})


def is_crypto_ticker(ticker: str) -> bool:
    return ticker.strip().upper().endswith(_CRYPTO_SUFFIX)


def to_alpaca_symbol(ticker: str) -> str:
    upper = ticker.strip().upper()
    if upper.endswith(_US_SUFFIX):
        base = upper[: -len(_US_SUFFIX)]
        if not _OUR_EQUITY_BASE.match(base):
            raise UnsupportedTickerError(f"ticker {ticker!r} is not a valid US equity ticker")
        return base.replace("-", ".")
    if upper.endswith(_CRYPTO_SUFFIX):
        pair = upper[: -len(_CRYPTO_SUFFIX)]
        legs = pair.split("-")
        if len(legs) != 2 or not all(_CRYPTO_LEG.match(leg) for leg in legs):
            raise UnsupportedTickerError(
                f"crypto ticker {ticker!r} must look like '<BASE>-<QUOTE>.CC' (e.g. BTC-USD.CC)"
            )
        return f"{legs[0]}/{legs[1]}"
    raise UnsupportedTickerError(
        f"ticker {ticker!r} cannot be traded at Alpaca: only US equities ('<SYMBOL>.US') "
        "and crypto pairs ('<BASE>-<QUOTE>.CC') are supported"
    )


def from_alpaca_symbol(symbol: str, *, asset_class: str | None = None) -> str:
    upper = symbol.strip().upper()
    klass = (asset_class or "").lower() or None
    if "/" in upper or klass in _CRYPTO_CLASSES:
        return _crypto_from_alpaca(symbol, upper)
    if klass is not None and klass not in _EQUITY_CLASSES:
        raise UnsupportedTickerError(
            f"Alpaca asset class {asset_class!r} ({symbol!r}) is not supported"
        )
    if not _ALPACA_EQUITY.match(upper):
        raise UnsupportedTickerError(
            f"Alpaca symbol {symbol!r} is not a US equity or crypto symbol"
        )
    return upper.replace(".", "-") + _US_SUFFIX


def _crypto_from_alpaca(symbol: str, upper: str) -> str:
    if "/" in upper:
        base, _, quote = upper.partition("/")
    else:
        quote = next((q for q in _CRYPTO_QUOTES if upper.endswith(q) and len(upper) > len(q)), "")
        base = upper[: -len(quote)] if quote else ""
    if not (_CRYPTO_LEG.match(base) and _CRYPTO_LEG.match(quote)):
        raise UnsupportedTickerError(f"Alpaca crypto symbol {symbol!r} is not a recognizable pair")
    return f"{base}-{quote}{_CRYPTO_SUFFIX}"

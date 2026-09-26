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


# ---- canonical tickers for synced holdings (roadmap 15.3) ------------------
#
# Broker connections report holdings by the venue's own symbol plus an
# exchange code. These helpers map them to our tickers and answer ``None``
# (never raise) for anything not covered, so a sync keeps the holding with
# its raw symbol instead of dropping it.

#: Exchange codes (vendor names and ISO 10383 MICs) -> our ticker suffix.
_EXCHANGE_SUFFIX: dict[str, str] = {
    **dict.fromkeys(
        (
            "NYSE", "NASDAQ", "NYSEARCA", "ARCA", "AMEX", "NYSEAMERICAN", "NYSEMKT",
            "BATS", "CBOE", "CBOEBZX", "IEX", "OTC", "OTCMKTS", "PINK", "US",
            "XNYS", "XNAS", "XNGS", "XNMS", "XNCM", "ARCX", "XASE", "XCBO", "IEXG",
            "OTCM", "XOTC",
        ),
        "US",
    ),
    **dict.fromkeys(("TSX", "XTSE", "TO"), "TO"),
    **dict.fromkeys(("TSXV", "XTSX", "V", "CVE"), "V"),
    **dict.fromkeys(("LSE", "XLON", "L"), "LSE"),
    **dict.fromkeys(("XETRA", "XETR", "DE"), "XETRA"),
    **dict.fromkeys(("ASX", "XASX", "AX", "AU"), "AU"),
}  # fmt: skip
#: Vendor suffixes a symbol may already carry (``SHOP.TO``), per our suffix.
_VENDOR_SUFFIXES: dict[str, tuple[str, ...]] = {
    "TO": (".TO",),
    "V": (".V", ".VN"),
    "LSE": (".L", ".LSE"),
    "XETRA": (".DE", ".XETRA"),
    "AU": (".AX", ".AU"),
    "US": (".US",),
}
_EQUITY_LIKE = frozenset({"equity", "etf", "fund", "adr", "reit"})
_LISTED_BASE = re.compile(r"^[A-Z0-9]{1,6}(-[A-Z0-9]{1,2})?$")


def to_canonical_ticker(
    symbol: str,
    *,
    exchange: str | None = None,
    asset_type: str | None = None,
    currency: str | None = None,
) -> str | None:
    """Our ticker for a venue ``symbol``, or ``None`` when not covered.

    ``asset_type`` is a normalized tag (``equity``, ``etf``, ``fund``,
    ``crypto``, ``option``, ``bond``, ...; ``None`` = assume a listed
    security). Crypto needs a quote currency, from a ``BASE/QUOTE`` symbol
    or from ``currency``. Without an exchange code only USD listings are
    assumed to be US-listed.
    """
    upper = (symbol or "").strip().upper()
    if not upper:
        return None
    kind = (asset_type or "").strip().lower() or None
    if kind == "crypto":
        return _canonical_crypto(upper, currency)
    if kind is not None and kind not in _EQUITY_LIKE:
        return None
    code = (exchange or "").strip().upper()
    if code:
        suffix = _EXCHANGE_SUFFIX.get(code)
    else:
        suffix = "US" if (currency or "").strip().upper() == "USD" else None
    if suffix is None:
        return None
    for vendor in _VENDOR_SUFFIXES.get(suffix, ()):
        if upper.endswith(vendor):
            upper = upper[: -len(vendor)]
            break
    base = upper.replace("/", "-").replace(".", "-")
    if not _LISTED_BASE.match(base):
        return None
    return f"{base}.{suffix}"


def _canonical_crypto(upper: str, currency: str | None) -> str | None:
    for sep in ("/", "-"):
        if sep in upper:
            base, _, quote = upper.partition(sep)
            break
    else:
        base, quote = upper, (currency or "").strip().upper()
    if not (_CRYPTO_LEG.match(base) and _CRYPTO_LEG.match(quote)):
        return None
    return f"{base}-{quote}{_CRYPTO_SUFFIX}"


def alpaca_symbol_to_ticker(symbol: str, *, asset_class: str | None = None) -> str | None:
    """:func:`from_alpaca_symbol` that answers ``None`` instead of raising."""
    try:
        return from_alpaca_symbol(symbol, asset_class=asset_class)
    except UnsupportedTickerError:
        return None

"""The profile a price ingest records for a ticker it has no profile for.

A trading run skips a ticker whose asset class it does not know. A
price-only install (Yahoo prices and no metadata run) would get no
signals, so every bar run that stores bars for such a ticker records this
minimal row: the id, the exchange and currency read from the ticker, and
the asset class. A metadata ingest later fills in the rest, and an
existing profile is never overwritten (see
:meth:`DuckDBLake.insert_missing_instruments`).
"""

from __future__ import annotations

import re

from stonks.core.types import AssetClass
from stonks.ingest.schemas import TickerProfile
from stonks.ingest.sources.eodhd import classify_asset_class

#: Exchange suffix -> the currency its prices are quoted in. A suffix not
#: listed here gets no currency, which reads the same as an unknown one and
#: leaves prices unscaled. London is left out on purpose: its lines quote in
#: pence (GBX), pounds or dollars (CSPX.LSE), and a wrong GBX would make
#: production read a dollar price as pence (``fx.units.price_scales``).
#: A metadata ingest sets the real unit.
_EXCHANGE_CURRENCY: dict[str, str] = {
    "US": "USD",
    **dict.fromkeys(("XETRA", "F", "PA", "AS", "BR", "LS", "MC", "MI", "VI", "HE", "IR"), "EUR"),
    "SW": "CHF",
    "CO": "DKK",
    "ST": "SEK",
    "OL": "NOK",
    "TO": "CAD",
    "V": "CAD",
    "AU": "AUD",
    "HK": "HKD",
    "SHG": "CNY",
    "SHE": "CNY",
    "KO": "KRW",
    "KQ": "KRW",
    "NSE": "INR",
    "BSE": "INR",
    "SA": "BRL",
    "MX": "MXN",
}

#: A crypto pair such as ``BTC-USD`` (the quote leg is the currency).
_CRYPTO_PAIR = re.compile(r"^[A-Z0-9]{2,12}-(USD|USDT|USDC|EUR|GBP|BTC|ETH)$")


def minimal_profile(ticker: str, *, source_id: str | None = None) -> TickerProfile:
    """The minimal profile of ``ticker``. ``source_id`` names the source the
    bars came from: both bar sources use the canonical ticker form, so the
    ticker alone decides the fields today."""
    base, _, suffix = ticker.rpartition(".")
    if not base:
        base, suffix = ticker, ""
    exchange = suffix.upper() or None
    pair = _CRYPTO_PAIR.match(base.upper())
    asset_class: AssetClass = classify_asset_class(ticker)
    currency = _EXCHANGE_CURRENCY.get(exchange or "")
    if pair is not None and asset_class == "equity":
        asset_class = "crypto"
    if asset_class == "crypto":
        currency = pair.group(1) if pair is not None else None
    return TickerProfile(id=ticker, asset_class=asset_class, exchange=exchange, currency=currency)

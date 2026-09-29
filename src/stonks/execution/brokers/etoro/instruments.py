"""eToro instrument ids to our tickers, and back.

eToro names every instrument by a numeric ``instrumentId`` that never
changes, and advises fetching each id once and keeping the mapping (Find
Instrument ID guide). :class:`InstrumentCatalog` does that in memory for
the process: it asks ``/api/v2/market-data/instruments`` only for ids or
symbols it has not seen. That is the caching the guide asks for, not a copy
of eToro's data (the terms forbid bulk caching).

Only markets Stonks already maps are covered: stocks and ETFs on the
exchanges of ``execution.brokers.symbols`` and crypto against USD.
Currencies, indices and commodities trade only as CFDs at eToro and are
"not covered" (``ticker`` is ``None``, the holding keeps eToro's symbol).
"""

from __future__ import annotations

import threading
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from typing import Any

from stonks.execution.brokers.base import UnsupportedTickerError
from stonks.execution.brokers.etoro.client import EtoroClient
from stonks.execution.brokers.symbols import to_canonical_ticker

_BATCH = 100

#: eToro instrument types -> our normalized asset types (others: not covered).
_KINDS: dict[str, str] = {"stocks": "equity", "etf": "etf", "crypto": "crypto"}

#: Words in eToro's exchange descriptions -> exchange codes ``symbols`` knows.
_EXCHANGE_WORDS: tuple[tuple[str, str], ...] = (
    ("NASDAQ", "NASDAQ"),
    ("NYSE", "NYSE"),
    ("NEW YORK", "NYSE"),
    ("LONDON", "LSE"),
    ("LSE", "LSE"),
    ("XETRA", "XETRA"),
    ("FRANKFURT", "XETRA"),
    ("TSX VENTURE", "TSXV"),
    ("TORONTO", "TSX"),
    ("TSX", "TSX"),
    ("ASX", "ASX"),
    ("AUSTRALIA", "ASX"),
)

#: Our ticker suffix -> the suffixes eToro's symbols may carry there.
_SYMBOL_SUFFIXES: dict[str, tuple[str, ...]] = {
    "US": ("",),
    "LSE": (".L",),
    "XETRA": (".DE",),
    "TO": (".TO",),
    "V": (".V",),
    "AU": (".AX", ".ASX"),
}


@dataclass(frozen=True)
class EtoroInstrument:
    id: int
    symbol: str
    type: str | None
    exchange: str | None
    name: str | None
    #: Our ticker, or ``None`` when Stonks does not cover it.
    ticker: str | None


def ticker_for(symbol: str, kind: str | None, exchange: str | None) -> str | None:
    """Our ticker for an eToro instrument, or ``None`` when not covered."""
    asset = _KINDS.get((kind or "").strip().lower())
    if asset is None or not symbol:
        return None
    if asset == "crypto":
        return to_canonical_ticker(symbol, asset_type="crypto", currency="USD")
    code = _exchange_code(exchange)
    if code is None:
        return None
    return to_canonical_ticker(symbol, exchange=code, asset_type=asset)


def _exchange_code(description: str | None) -> str | None:
    text = (description or "").strip().upper()
    if not text:
        return None
    for word, code in _EXCHANGE_WORDS:
        if word in text:
            return code
    return None


class InstrumentCatalog:
    """Instrument ids and tickers seen by this process, both ways."""

    def __init__(self, overrides: Mapping[str, int] | None = None) -> None:
        self._overrides = {k.strip().upper(): int(v) for k, v in (overrides or {}).items()}
        self._by_id: dict[int, EtoroInstrument] = {}
        self._by_ticker: dict[str, int] = {}
        self._exchanges: dict[int, str] | None = None
        self._lock = threading.Lock()

    def by_ids(self, client: EtoroClient, ids: Iterable[int]) -> dict[int, EtoroInstrument]:
        wanted = list(dict.fromkeys(int(i) for i in ids))
        with self._lock:
            missing = [i for i in wanted if i not in self._by_id]
        for start in range(0, len(missing), _BATCH):
            chunk = missing[start : start + _BATCH]
            rows = self._search(client, {"instrumentsIds": ",".join(str(i) for i in chunk)})
            found = {inst.id: inst for inst in self._to_instruments(client, rows)}
            with self._lock:
                for iid in chunk:
                    # an id eToro no longer lists stays "not covered"
                    self._keep(found.get(iid) or EtoroInstrument(iid, str(iid), None, None,
                                                                 None, None))  # fmt: skip
        with self._lock:
            return {i: self._by_id[i] for i in wanted}

    def resolve(self, client: EtoroClient, ticker: str) -> EtoroInstrument:
        """The instrument our ``ticker`` trades as, or
        :class:`UnsupportedTickerError`."""
        key = ticker.strip().upper()
        pinned = self._overrides.get(key)
        if pinned is not None:
            inst = self.by_ids(client, [pinned])[pinned]
            return EtoroInstrument(inst.id, inst.symbol, inst.type, inst.exchange, inst.name, key)
        with self._lock:
            known = self._by_ticker.get(key)
            if known is not None:
                return self._by_id[known]
        candidates = _symbol_candidates(key)
        if not candidates:
            raise UnsupportedTickerError(f"{ticker} cannot be traded at eToro through Stonks")
        rows = self._search(client, {"symbols": ",".join(candidates)})
        for inst in self._to_instruments(client, rows):
            with self._lock:
                self._keep(inst)
            if inst.ticker == key:
                return inst
        raise UnsupportedTickerError(f"eToro lists no instrument for {ticker}")

    # ---- plumbing ------------------------------------------------------------------------

    def _keep(self, inst: EtoroInstrument) -> None:
        self._by_id[inst.id] = inst
        if inst.ticker is not None:
            self._by_ticker.setdefault(inst.ticker, inst.id)

    def _search(self, client: EtoroClient, params: Mapping[str, str]) -> list[dict[str, Any]]:
        from stonks.connections.base import ProviderError

        try:
            data = client.get("instruments", "/api/v2/market-data/instruments",
                              {**params, "pageSize": _BATCH})  # fmt: skip
        except ProviderError as exc:
            if exc.status == 404:  # no instrument matched
                return []
            raise
        rows = data.get("results") if isinstance(data, dict) else None
        return [r for r in rows or [] if isinstance(r, dict)]

    def _to_instruments(
        self, client: EtoroClient, rows: list[dict[str, Any]]
    ) -> list[EtoroInstrument]:
        exchanges = self._exchange_names(client) if rows else {}
        out: list[EtoroInstrument] = []
        for row in rows:
            try:
                iid = int(row["instrumentId"])
            except (KeyError, TypeError, ValueError):
                continue
            symbol = str(row.get("symbol") or iid)
            kind = row.get("type")
            exchange = exchanges.get(_int(row.get("exchangeId")))
            out.append(
                EtoroInstrument(
                    id=iid,
                    symbol=symbol,
                    type=str(kind) if kind else None,
                    exchange=exchange,
                    name=row.get("displayName"),
                    ticker=ticker_for(symbol, kind, exchange),
                )
            )
        return out

    def _exchange_names(self, client: EtoroClient) -> dict[int, str]:
        with self._lock:
            if self._exchanges is not None:
                return self._exchanges
        data = client.get("exchanges", "/api/v1/market-data/exchanges")
        rows = data.get("exchangeInfo") if isinstance(data, dict) else None
        names = {
            _int(r.get("exchangeID")): str(r.get("exchangeDescription") or "")
            for r in rows or []
            if isinstance(r, dict)
        }
        with self._lock:
            self._exchanges = names
        return names


def _symbol_candidates(ticker: str) -> list[str]:
    """The symbols eToro may list our ``ticker`` under."""
    base, _, suffix = ticker.rpartition(".")
    if not base:
        return []
    if suffix == "CC":
        coin, _, quote = base.partition("-")
        return [coin] if quote == "USD" and coin else []
    endings = _SYMBOL_SUFFIXES.get(suffix)
    if endings is None:
        return []
    variants = [base, base.replace("-", "."), base.replace("-", "")]
    out = [f"{v}{end}" for end in endings for v in variants]
    return list(dict.fromkeys(out))


def _int(value: Any) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return -1


_CATALOGS: dict[tuple[str, tuple[tuple[str, int], ...]], InstrumentCatalog] = {}
_CATALOGS_LOCK = threading.Lock()


def catalog_for(base_url: str, overrides: Mapping[str, int] | None = None) -> InstrumentCatalog:
    """The one catalog of an API base URL (and overrides) in this process."""
    key = (base_url.rstrip("/"), tuple(sorted((overrides or {}).items())))
    with _CATALOGS_LOCK:
        catalog = _CATALOGS.get(key)
        if catalog is None:
            catalog = _CATALOGS[key] = InstrumentCatalog(overrides)
        return catalog


def reset_catalogs() -> None:
    """Forget every mapping (tests)."""
    with _CATALOGS_LOCK:
        _CATALOGS.clear()


__all__ = [
    "EtoroInstrument",
    "InstrumentCatalog",
    "catalog_for",
    "reset_catalogs",
    "ticker_for",
]

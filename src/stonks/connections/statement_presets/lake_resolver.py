"""Map ISINs to our tickers through the lake's ``instruments`` identifiers.

One ISIN can have many listings (``AAPL.US`` and ``APC.XETRA``). The pick,
in order: the listing on the export's exchange (by ticker suffix), then
the one in the export's currency, then the only one. More than one left
means the export cannot tell them apart, and the instrument stays by its
ISIN, flagged as not covered. Never a guess.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from contextlib import AbstractContextManager
from typing import Any

from stonks.connections.statement_presets.base import Listing
from stonks.logging import get_logger

_log = get_logger("stonks.connections.statement_presets.lake_resolver")

Candidate = tuple[str, str | None]  # (ticker, currency)


def pick_listing(listing: Listing, candidates: Sequence[Candidate]) -> str | None:
    """The one ticker among ``candidates`` for ``listing``, or ``None``."""
    if not candidates:
        return None
    if listing.suffix:
        on_exchange = [t for t, _ in candidates if t.upper().endswith(f".{listing.suffix}")]
        if len(on_exchange) == 1:
            return on_exchange[0]
        if on_exchange:
            candidates = [c for c in candidates if c[0] in on_exchange]
    if listing.currency:
        same = [t for t, c in candidates if (c or "").upper() == listing.currency.upper()]
        if len(same) == 1:
            return same[0]
        if same:
            candidates = [c for c in candidates if c[0] in same]
    if len(candidates) == 1:
        return candidates[0][0]
    return None


class LakeInstrumentResolver:
    """Reads ``instruments (id, isin, currency)``. A lake that cannot be
    opened (another process holds it, or it is not made yet) maps nothing:
    the import still works, by ISIN."""

    def __init__(self, open_lake: Callable[[], AbstractContextManager[Any]]) -> None:
        self._open_lake = open_lake

    def resolve(self, listings: Sequence[Listing]) -> Mapping[Listing, str | None]:
        isins = sorted({li.isin.upper() for li in listings if li.isin})
        by_isin: dict[str, list[Candidate]] = {}
        if isins:
            try:
                with self._open_lake() as lake:
                    df = lake.sql(
                        "SELECT id, upper(isin) AS isin, currency FROM instruments"
                        " WHERE upper(isin) IN (SELECT unnest(?)) ORDER BY id",
                        [isins],
                    )
                for row in df.to_dict("records"):
                    by_isin.setdefault(str(row["isin"]), []).append(
                        (str(row["id"]), row["currency"] if row["currency"] else None)
                    )
            except Exception as exc:  # an unreadable lake maps nothing
                _log.warning("statement_presets.lake_unavailable", error=type(exc).__name__)
        return {li: pick_listing(li, by_isin.get(li.isin.upper(), [])) for li in listings}

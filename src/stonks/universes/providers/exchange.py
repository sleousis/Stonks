"""``exchange`` universes: every symbol a data source lists on an exchange.

The source's :meth:`~stonks.ingest.sources.base.DataSource.list_symbols`
gives active and delisted symbols with their security type. Each kept
symbol becomes one span:

* it starts on the instrument's ``ipo_date``, else its first daily bar in
  the lake, else ``start_date`` (default 1900-01-01);
* an active symbol stays open; a delisted one ends on its
  ``delisted_date``, else the day after its last daily bar, else (no dates
  at all yet) on the refresh date, with a warning. Ensure its data and
  refresh again to tighten such spans.

The listings also reach the ``instruments`` table (asset class, venue,
currency, name, security type, ISIN, delisted flag), so the lab preflight
and rule universes can see them. A later richer profile still wins where
it has values.
"""

from __future__ import annotations

from datetime import date, timedelta

import pandas as pd
from pydantic import BaseModel, ConfigDict, Field

from stonks.core.interval import Interval
from stonks.ingest.schemas import SecurityType, SymbolListing, TickerProfile
from stonks.universes.base import (
    EARLIEST,
    Materialized,
    MembershipSpan,
    RefreshContext,
    UniverseProvider,
)

#: Tickers named in a warning before it says "and N more".
_SHOWN = 5


class ExchangeSpec(BaseModel):
    model_config = ConfigDict(extra="forbid")

    #: The source's exchange code, e.g. ``US``, ``LSE``, ``CC``.
    exchange: str = Field(min_length=1, max_length=32)
    #: Data source id (default: the configured default source).
    source: str | None = None
    #: Keep only these equity kinds; ``None`` keeps every listing.
    security_types: list[SecurityType] | None = None
    include_delisted: bool = True
    #: Listing date for symbols with no known date and no bars.
    start_date: date = EARLIEST


class ExchangeProvider(UniverseProvider):
    kind = "exchange"
    spec_model = ExchangeSpec

    def materialize(self, spec: ExchangeSpec, ctx: RefreshContext) -> Materialized:
        if ctx.source is None:
            raise ValueError("an exchange universe needs a data source to list symbols")
        listings = [
            s
            for s in ctx.source(spec.source).list_symbols(spec.exchange)
            if (spec.include_delisted or not s.is_delisted)
            and (spec.security_types is None or s.security_type in spec.security_types)
        ]
        listings = list({s.ticker: s for s in listings}.values())
        dates = _known_dates(ctx.lake, [s.ticker for s in listings])
        spans: list[MembershipSpan] = []
        undated: list[str] = []
        skipped: list[str] = []
        for s in listings:
            ipo, delisted, first_bar, last_bar = dates.get(s.ticker, (None, None, None, None))
            start = ipo or first_bar or spec.start_date
            end: date | None = None
            if s.is_delisted:
                if delisted is not None:
                    end = delisted
                elif last_bar is not None:
                    end = last_bar + timedelta(days=1)
                else:
                    end = ctx.as_of
                    undated.append(s.ticker)
            if end is not None and end <= start:
                skipped.append(s.ticker)
                continue
            spans.append(MembershipSpan(s.ticker, start, end))
        warnings: list[str] = []
        if undated:
            warnings.append(
                f"{len(undated)} delisted symbols have no listing dates and no bars, so they are "
                f"members up to {ctx.as_of}: {_names(undated)}. Ensure their data and refresh "
                "again to tighten their spans"
            )
        if skipped:
            warnings.append(
                f"{len(skipped)} symbols were delisted before they listed, skipped: "
                f"{_names(skipped)}"
            )
        return Materialized(
            spans=spans, warnings=warnings, profiles=[_profile(s) for s in listings]
        )


def _names(tickers: list[str]) -> str:
    extra = len(tickers) - _SHOWN
    shown = ", ".join(tickers[:_SHOWN])
    return f"{shown} and {extra} more" if extra > 0 else shown


def _profile(s: SymbolListing) -> TickerProfile:
    return TickerProfile(
        id=s.ticker,
        asset_class=s.asset_class,
        exchange=s.exchange,
        currency=s.currency,
        name=s.name,
        is_delisted=s.is_delisted,
        security_type=s.security_type,
        isin=s.isin,
    )


def _known_dates(
    lake, tickers: list[str]
) -> dict[str, tuple[date | None, date | None, date | None, date | None]]:
    """``{ticker: (ipo_date, delisted_date, first daily bar, last daily bar)}``."""
    if not tickers:
        return {}
    df = lake.con.execute(
        """
        WITH wanted AS (SELECT UNNEST(?) AS ticker),
        span AS (
            SELECT b.ticker, MIN(b.timestamp) AS first_bar, MAX(b.timestamp) AS last_bar
              FROM bars b JOIN wanted w ON w.ticker = b.ticker
             WHERE b.interval = ? GROUP BY b.ticker
        )
        SELECT w.ticker, i.ipo_date, i.delisted_date, s.first_bar, s.last_bar
          FROM wanted w
          LEFT JOIN instruments i ON i.id = w.ticker
          LEFT JOIN span s ON s.ticker = w.ticker
        """,
        [list(tickers), str(Interval.DAY_1)],
    ).fetchdf()
    out = {}
    for r in df.itertuples(index=False):
        out[r.ticker] = tuple(
            None if pd.isna(v) else pd.Timestamp(v).date()
            for v in (r.ipo_date, r.delisted_date, r.first_bar, r.last_bar)
        )
    return out

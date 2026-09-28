"""MarketDataService — instruments, bars and per-ticker data coverage."""

from __future__ import annotations

from datetime import date, datetime
from typing import Any

import pandas as pd
from pydantic import BaseModel, Field

from stonks.app.context import AppContext
from stonks.app.errors import ValidationError
from stonks.app.pagination import Page
from stonks.app.serialize import finite
from stonks.breadth import Breadth
from stonks.core.interval import Interval
from stonks.core.timeutil import day_end, day_start

DEFAULT_BAR_LIMIT = 5_000
MAX_BAR_LIMIT = 50_000


class InstrumentView(BaseModel):
    id: str
    name: str | None
    asset_class: str | None
    exchange: str | None
    currency: str | None
    sector: str | None
    industry: str | None
    is_delisted: bool | None


class BarView(BaseModel):
    timestamp: datetime
    open: float | None
    high: float | None
    low: float | None
    close: float | None
    adj_close: float | None
    volume: float | None


class BarSeries(BaseModel):
    ticker: str
    interval: str
    bars: list[BarView]
    #: True when the window held more bars than ``limit``; the most recent
    #: ``limit`` bars are returned.
    truncated: bool


class CoverageRow(BaseModel):
    ticker: str
    interval: str
    first_bar: datetime
    last_bar: datetime
    rows: int


class MarketDataService:
    def __init__(self, context: AppContext) -> None:
        self._ctx = context

    def instruments(
        self,
        *,
        q: str | None = None,
        asset_class: str | None = None,
        limit: int,
        offset: int,
    ) -> Page[InstrumentView]:
        where: list[str] = []
        params: list[Any] = []
        if q:
            where.append("(id ILIKE ? ESCAPE '!' OR name ILIKE ? ESCAPE '!')")
            pattern = f"%{_escape_like(q)}%"
            params += [pattern, pattern]
        if asset_class:
            where.append("asset_class = ?")
            params.append(asset_class)
        clause = f" WHERE {' AND '.join(where)}" if where else ""
        with self._ctx.lake() as lake:
            total = int(lake.sql(f"SELECT COUNT(*) AS n FROM instruments{clause}", params).n[0])
            df = lake.sql(
                "SELECT id, name, asset_class, exchange, currency, sector, industry, is_delisted "
                f"FROM instruments{clause} ORDER BY id LIMIT ? OFFSET ?",
                [*params, limit, offset],
            )
        items = [InstrumentView(**_clean(r)) for r in df.to_dict(orient="records")]
        return Page[InstrumentView](items=items, total=total, limit=limit, offset=offset)

    def bars(
        self,
        ticker: str,
        *,
        interval: str = "1d",
        start: date | datetime | None = None,
        end: date | datetime | None = None,
        limit: int = DEFAULT_BAR_LIMIT,
    ) -> BarSeries:
        iv = _parse_interval(interval)
        if not 1 <= limit <= MAX_BAR_LIMIT:
            raise ValidationError(f"limit must be between 1 and {MAX_BAR_LIMIT}")
        lo = day_start(start) if start is not None else datetime(1900, 1, 1)
        hi = day_end(end) if end is not None else datetime(2200, 1, 1)
        with self._ctx.lake() as lake:
            # newest ``limit + 1`` rows, so we can tell whether we truncated
            df = lake.sql(
                """
                SELECT timestamp, open, high, low, close, adj_close, volume FROM bars
                 WHERE ticker = ? AND interval = ? AND timestamp BETWEEN ? AND ?
                 ORDER BY timestamp DESC LIMIT ?
                """,
                [ticker, iv.code, lo, hi, limit + 1],
            )
        truncated = len(df) > limit
        records = df.head(limit).to_dict(orient="records")[::-1]
        bars = [
            BarView(
                timestamp=pd.Timestamp(r["timestamp"]).to_pydatetime(),
                **{k: finite(_nan_to_none(r[k])) for k in _PRICE_COLS},
            )
            for r in records
        ]
        return BarSeries(ticker=ticker, interval=iv.code, bars=bars, truncated=truncated)

    def coverage(
        self,
        *,
        ticker: str | None = None,
        interval: str | None = None,
        limit: int,
        offset: int,
    ) -> Page[CoverageRow]:
        where: list[str] = []
        params: list[Any] = []
        if ticker:
            where.append("ticker = ?")
            params.append(ticker)
        if interval:
            where.append("interval = ?")
            params.append(_parse_interval(interval).code)
        clause = f" WHERE {' AND '.join(where)}" if where else ""
        with self._ctx.lake() as lake:
            total = int(
                lake.sql(
                    f"SELECT COUNT(*) AS n FROM (SELECT DISTINCT ticker, interval FROM bars{clause})",
                    params,
                ).n[0]
            )
            df = lake.sql(
                "SELECT ticker, interval, MIN(timestamp) AS first_bar, MAX(timestamp) AS last_bar, "
                f"COUNT(*) AS rows FROM bars{clause} GROUP BY ticker, interval "
                "ORDER BY ticker, interval LIMIT ? OFFSET ?",
                [*params, limit, offset],
            )
        items = [
            CoverageRow(
                ticker=r["ticker"],
                interval=r["interval"],
                first_bar=pd.Timestamp(r["first_bar"]).to_pydatetime(),
                last_bar=pd.Timestamp(r["last_bar"]).to_pydatetime(),
                rows=int(r["rows"]),
            )
            for r in df.to_dict(orient="records")
        ]
        return Page[CoverageRow](items=items, total=total, limit=limit, offset=offset)

    # ---- breadth (roadmap 23.14) -----------------------------------------------

    def breadth(self, *, as_of: date | None = None) -> BreadthView:
        """Market breadth on the last day with bars on or before ``as_of``:
        advances and declines, the share above the 50 and 200 day averages,
        new highs and lows and distribution days on the index. Display
        only."""
        from datetime import timedelta

        from stonks.breadth import market_breadth

        settings = self._ctx.settings.breadth
        with self._ctx.lake() as lake:
            end = as_of or _last_price_day(lake)
            if end is None:
                empty = pd.DataFrame(columns=["ticker", "date", "close", "volume"])
                return BreadthView(
                    universe=_universe_label(settings.universe_id),
                    breadth=market_breadth(empty, None, settings=settings),
                )
            start = end - timedelta(days=settings.lookback_days)
            members = self._breadth_members(lake, settings, end)
            bars = (
                lake.sql(
                    "SELECT ticker, date, COALESCE(adj_close, close) AS close FROM prices"
                    " WHERE ticker = ANY(?) AND date > ? AND date <= ?",
                    [members, start, end],
                )
                if members
                else pd.DataFrame(columns=["ticker", "date", "close"])
            )
            index = (
                lake.sql(
                    "SELECT date, COALESCE(adj_close, close) AS close, volume FROM prices"
                    " WHERE ticker = ? AND date > ? AND date <= ? ORDER BY date",
                    [settings.index, start, end],
                )
                if settings.index
                else None
            )
        return BreadthView(
            universe=_universe_label(settings.universe_id),
            breadth=market_breadth(bars, index, settings=settings, as_of=end),
        )

    @staticmethod
    def _breadth_members(lake: Any, settings: Any, day: date) -> list[str]:
        """The stored universe's members on ``day``, else every equity in
        the lake. Funds with a holdings list and the index are left out."""
        if settings.universe_id:
            return sorted(lake.members_as_of(settings.universe_id, day))
        df = lake.sql(
            "SELECT id FROM instruments WHERE asset_class = 'equity'"
            " AND id NOT IN (SELECT DISTINCT fund FROM fund_holdings)"
            " AND id IS DISTINCT FROM ?",
            [settings.index],
        )
        return sorted(str(t) for t in df["id"])


_PRICE_COLS = ("open", "high", "low", "close", "adj_close", "volume")


def _parse_interval(code: str) -> Interval:
    try:
        return Interval.parse(code)
    except (ValueError, TypeError) as exc:
        raise ValidationError(f"invalid interval {code!r}: {exc}") from None


def _escape_like(text: str) -> str:
    """Match user text literally inside ``ILIKE ... ESCAPE '!'``."""
    return text.replace("!", "!!").replace("%", "!%").replace("_", "!_")


def _nan_to_none(value: Any) -> Any:
    if value is None:
        return None
    try:
        if pd.isna(value):
            return None
    except (TypeError, ValueError):
        pass
    return value


def _clean(record: dict[str, Any]) -> dict[str, Any]:
    out = {k: _nan_to_none(v) for k, v in record.items()}
    if out.get("is_delisted") is not None:
        out["is_delisted"] = bool(out["is_delisted"])
    return out


class BreadthView(BaseModel):
    universe: str = Field(description="What was measured, in words.")
    breadth: Breadth


def _universe_label(universe_id: str | None) -> str:
    return f"universe {universe_id}" if universe_id else "every stock in the lake"


def _last_price_day(lake: Any) -> date | None:
    df = lake.sql("SELECT max(date) AS d FROM prices")
    value = df["d"].iloc[0] if not df.empty else None
    if value is None or pd.isna(value):
        return None
    day = pd.Timestamp(value)
    return date.fromisoformat(str(day.date()))

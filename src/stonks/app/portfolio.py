"""PortfolioService — the book as of the latest tick, valued at the latest
stored closes, plus snapshot history."""

from __future__ import annotations

import json
from datetime import date, datetime

from pydantic import BaseModel

from stonks.app.context import AppContext
from stonks.app.pagination import Page


class PositionView(BaseModel):
    ticker: str
    quantity: float
    price: float | None
    price_date: date | None
    market_value: float | None
    weight: float | None


class PortfolioView(BaseModel):
    #: When the underlying snapshot was taken; ``None`` before the first tick
    #: (the book is then the configured initial cash).
    taken_at: datetime | None
    tick_id: str | None
    cash: float
    positions: list[PositionView]
    positions_value: float
    total_value: float
    #: Total value recorded at snapshot time (valued at that tick's prices).
    snapshot_total_value: float | None


class SnapshotView(BaseModel):
    id: int
    tick_id: str | None
    taken_at: datetime
    cash: float
    positions: dict[str, float]
    total_value: float


class PortfolioService:
    def __init__(self, context: AppContext) -> None:
        self._ctx = context

    def current(self) -> PortfolioView:
        with self._ctx.state() as state:
            rows = state.sql("SELECT * FROM portfolio_snapshots ORDER BY id DESC LIMIT 1")
        if not rows:
            cash = float(self._ctx.settings.production.initial_cash)
            return PortfolioView(
                taken_at=None,
                tick_id=None,
                cash=cash,
                positions=[],
                positions_value=0.0,
                total_value=cash,
                snapshot_total_value=None,
            )
        row = rows[0]
        holdings: dict[str, float] = json.loads(row["positions_json"])
        latest = self._latest_closes(list(holdings))
        cash = float(row["cash"])

        positions: list[PositionView] = []
        for ticker in sorted(holdings):
            qty = float(holdings[ticker])
            price, price_date = latest.get(ticker, (None, None))
            positions.append(
                PositionView(
                    ticker=ticker,
                    quantity=qty,
                    price=price,
                    price_date=price_date,
                    market_value=None if price is None else qty * price,
                    weight=None,
                )
            )
        positions_value = sum(p.market_value or 0.0 for p in positions)
        total = cash + positions_value
        for p in positions:
            if p.market_value is not None and total:
                p.weight = p.market_value / total
        return PortfolioView(
            taken_at=datetime.fromisoformat(row["taken_at"]),
            tick_id=row["tick_id"],
            cash=cash,
            positions=positions,
            positions_value=positions_value,
            total_value=total,
            snapshot_total_value=float(row["total_value"]),
        )

    def snapshots(self, *, limit: int, offset: int) -> Page[SnapshotView]:
        with self._ctx.state() as state:
            total = int(state.sql("SELECT COUNT(*) FROM portfolio_snapshots")[0][0])
            rows = state.sql(
                "SELECT * FROM portfolio_snapshots ORDER BY id DESC LIMIT ? OFFSET ?",
                [limit, offset],
            )
        items = [
            SnapshotView(
                id=r["id"],
                tick_id=r["tick_id"],
                taken_at=datetime.fromisoformat(r["taken_at"]),
                cash=float(r["cash"]),
                positions=json.loads(r["positions_json"]),
                total_value=float(r["total_value"]),
            )
            for r in rows
        ]
        return Page[SnapshotView](items=items, total=total, limit=limit, offset=offset)

    def _latest_closes(self, tickers: list[str]) -> dict[str, tuple[float, date]]:
        if not tickers:
            return {}
        with self._ctx.lake() as lake:
            df = lake.sql(
                """
                SELECT ticker, max(date) AS date, arg_max(close, date) AS close
                  FROM prices
                 WHERE ticker = ANY(?)
                 GROUP BY ticker
                """,
                [tickers],
            )
        return {
            r.ticker: (float(r.close), r.date.date() if isinstance(r.date, datetime) else r.date)
            for r in df.itertuples(index=False)
        }

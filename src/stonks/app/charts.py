"""ChartService: everything a price chart of one ticker needs (roadmap 13.5).

One read returns the bars, the caller's own fills of that ticker (marked B
and S on the chart) and the strategies' signal events for it (entries,
exits, size changes, with their plain reason). Moving averages are drawn by
the client from the bars, so the server stays a plain read.

Fills come from one of the caller's portfolios, resolved through
``PortfolioService.resolve`` by the route: another person's portfolio is a
404, and someone without a portfolio simply gets no fills. Signals are
global, like the strategy catalog.
"""

from __future__ import annotations

import json
from datetime import date, datetime

from pydantic import BaseModel

from stonks.app.context import AppContext
from stonks.app.market import BarView, MarketDataService
from stonks.app.serialize import finite
from stonks.production.ledger import ledger_filter

#: Bars a chart asks for by default (about three years of days), and at most.
DEFAULT_CHART_BARS = 750
MAX_CHART_BARS = 5_000
#: Most fills and signal events returned with one chart.
MAX_MARKERS = 1_000


class ChartFillView(BaseModel):
    filled_at: datetime
    #: ``buy`` or ``sell``; null when the order is gone.
    side: str | None
    quantity: float
    price: float
    order_client_id: str
    strategy_id: str | None


class ChartSignalView(BaseModel):
    as_of: date
    strategy_id: str
    #: ``entry``, ``exit``, ``increase``, ``decrease`` or ``risk``.
    kind: str
    strength: float | None
    #: The strategy's plain reason, when it gave one.
    reason: str | None


class ChartView(BaseModel):
    ticker: str
    interval: str
    bars: list[BarView]
    #: True when the window held more bars than ``limit``.
    truncated: bool
    #: The portfolio the fills come from; null when there is none.
    portfolio_id: str | None
    fills: list[ChartFillView]
    signals: list[ChartSignalView]


class ChartService:
    def __init__(self, context: AppContext) -> None:
        self._ctx = context
        self._market = MarketDataService(context)

    def chart(
        self,
        ticker: str,
        *,
        portfolio_id: str | None,
        interval: str = "1d",
        start: date | None = None,
        end: date | None = None,
        limit: int = DEFAULT_CHART_BARS,
        strategy_id: str | None = None,
    ) -> ChartView:
        """``portfolio_id`` must already be resolved against the caller
        (or ``None`` for no fills)."""
        series = self._market.bars(ticker, interval=interval, start=start, end=end, limit=limit)
        first = series.bars[0].timestamp if series.bars else None
        last = series.bars[-1].timestamp if series.bars else None
        fills: list[ChartFillView] = []
        signals: list[ChartSignalView] = []
        if first is not None and last is not None:
            lo, hi = first.date().isoformat(), last.date().isoformat()
            with self._ctx.state() as state:
                if portfolio_id is not None:
                    where, params = ledger_filter(state, "fills", portfolio_id, alias="f")
                    rows = state.sql(
                        "SELECT f.filled_at, f.quantity, f.price, f.order_client_id,"
                        " o.side AS side, o.strategy_id AS strategy_id"
                        " FROM fills f LEFT JOIN orders o ON o.client_id = f.order_client_id"
                        " WHERE f.ticker = ? AND substr(f.filled_at, 1, 10) BETWEEN ? AND ?"
                        f" AND {where} ORDER BY f.filled_at, f.id LIMIT ?",
                        [ticker, lo, hi, *params, MAX_MARKERS],
                    )
                    fills = [
                        ChartFillView(
                            filled_at=datetime.fromisoformat(r["filled_at"]),
                            side=r["side"],
                            quantity=float(r["quantity"]),
                            price=float(r["price"]),
                            order_client_id=r["order_client_id"],
                            strategy_id=r["strategy_id"],
                        )
                        for r in rows
                    ]
                clause = " AND strategy_id = ?" if strategy_id else ""
                extra = [strategy_id] if strategy_id else []
                rows = state.sql(
                    "SELECT as_of, strategy_id, kind, strength, reason_json FROM signal_events"
                    f" WHERE ticker = ? AND as_of BETWEEN ? AND ?{clause}"
                    " ORDER BY as_of, id LIMIT ?",
                    [ticker, lo, hi, *extra, MAX_MARKERS],
                )
                signals = [
                    ChartSignalView(
                        as_of=date.fromisoformat(r["as_of"]),
                        strategy_id=r["strategy_id"],
                        kind=r["kind"],
                        strength=finite(r["strength"]),
                        reason=_reason_text(r["reason_json"]),
                    )
                    for r in rows
                ]
        return ChartView(
            ticker=series.ticker,
            interval=series.interval,
            bars=series.bars,
            truncated=series.truncated,
            portfolio_id=portfolio_id,
            fills=fills,
            signals=signals,
        )


def _reason_text(raw: str | None) -> str | None:
    try:
        text = json.loads(raw or "{}").get("text")
    except (ValueError, AttributeError):
        return None
    return str(text) if text else None

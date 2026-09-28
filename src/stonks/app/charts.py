"""ChartService: everything a price chart of one ticker needs (roadmap 13.5).

One read returns the bars, the caller's own fills of that ticker (marked B
and S on the chart) and the strategies' signal events for it (entries,
exits, size changes, with their plain reason). Moving averages are drawn by
the client from the bars, so the server stays a plain read.

``compare`` puts several tickers on one scale: each rebased to 100 on the
first day they all have a price, with its drawdown from the running peak
and its rolling Sharpe, for the compare chart and the performance panel.

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
from stonks.app.errors import ValidationError
from stonks.app.market import BarView, MarketDataService
from stonks.app.serialize import finite
from stonks.production.ledger import ledger_filter
from stonks.reporting.tearsheet import rolling_sharpe

#: Bars a chart asks for by default (about three years of days), and at most.
DEFAULT_CHART_BARS = 750
MAX_CHART_BARS = 5_000
#: Most fills and signal events returned with one chart.
MAX_MARKERS = 1_000
#: Most tickers on one compare chart (the categorical line styles, six).
MAX_COMPARE = 6
#: Bars behind each rolling Sharpe point by default (about three months).
DEFAULT_ROLLING_WINDOW = 63
MIN_ROLLING_WINDOW, MAX_ROLLING_WINDOW = 5, 252
#: Value every compared series starts at.
REBASE = 100.0


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


class ComparePoint(BaseModel):
    time: date
    value: float


class CompareSeriesView(BaseModel):
    ticker: str
    #: Adjusted close rebased to 100 on the compare start.
    points: list[ComparePoint]
    #: Fall from the running peak since the start (0 at a peak, -0.2 is 20% down).
    drawdown: list[ComparePoint]
    #: Annualized Sharpe of the trailing ``window`` daily returns. Uses the
    #: bars before the start too, so it begins on the start when they exist.
    rolling_sharpe: list[ComparePoint]
    total_return: float | None
    max_drawdown: float | None
    #: The latest rolling Sharpe.
    sharpe: float | None
    #: Trading days per year behind the annualization (365 for crypto).
    periods_per_year: float


class CompareView(BaseModel):
    #: The first day every ticker has a price; null when none has one.
    start: date | None
    end: date | None
    window: int
    series: list[CompareSeriesView]
    #: Tickers with no daily price in the range.
    missing: list[str]


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

    def compare(
        self,
        tickers: list[str],
        *,
        start: date | None = None,
        end: date | None = None,
        limit: int = DEFAULT_CHART_BARS,
        window: int = DEFAULT_ROLLING_WINDOW,
    ) -> CompareView:
        """Daily adjusted closes of ``tickers`` (the latest ``limit`` bars
        each) on one scale, with drawdown and rolling Sharpe. The ``window``
        bars before those feed the first rolling Sharpe points."""
        names = _compare_tickers(tickers)
        if not MIN_ROLLING_WINDOW <= window <= MAX_ROLLING_WINDOW:
            raise ValidationError(
                f"window must be between {MIN_ROLLING_WINDOW} and {MAX_ROLLING_WINDOW} bars"
            )
        if not 1 <= limit <= MAX_CHART_BARS:
            raise ValidationError(f"limit must be between 1 and {MAX_CHART_BARS}")
        closes: dict[str, list[tuple[date, float]]] = {}
        missing: list[str] = []
        for name in names:
            # ``window`` extra bars so the rolling Sharpe starts on the first shown day
            series = self._market.bars(
                name, interval="1d", start=start, end=end, limit=limit + window
            )
            rows = [
                (b.timestamp.date(), float(price))
                for b in series.bars
                if (price := b.adj_close if b.adj_close is not None else b.close) is not None
                and price > 0
            ]
            if rows:
                closes[name] = rows
            else:
                missing.append(name)
        if not closes:
            return CompareView(start=None, end=None, window=window, series=[], missing=missing)
        common = max(rows[-limit:][0][0] for rows in closes.values())
        crypto = self._crypto(list(closes))
        out = [
            _compare_series(name, rows, common, window, 365.0 if name in crypto else 252.0)
            for name, rows in closes.items()
        ]
        last = max(rows[-1][0] for rows in closes.values())
        return CompareView(start=common, end=last, window=window, series=out, missing=missing)

    def _crypto(self, tickers: list[str]) -> set[str]:
        with self._ctx.lake() as lake:
            df = lake.sql(
                "SELECT id FROM instruments WHERE id = ANY(?) AND asset_class = 'crypto'",
                [tickers],
            )
        return {str(i) for i in df["id"]}


def _compare_tickers(tickers: list[str]) -> list[str]:
    names: list[str] = []
    for raw in tickers:
        name = raw.strip().upper()
        if not name:
            continue
        if len(name) > 40:
            raise ValidationError(f"ticker {name[:40]}... is too long")
        if name not in names:
            names.append(name)
    if not names:
        raise ValidationError("name at least one ticker")
    if len(names) > MAX_COMPARE:
        raise ValidationError(f"compare at most {MAX_COMPARE} tickers")
    return names


def _compare_series(
    ticker: str, rows: list[tuple[date, float]], start: date, window: int, periods: float
) -> CompareSeriesView:
    shown = [(d, v) for d, v in rows if d >= start]
    base = shown[0][1]
    points: list[ComparePoint] = []
    drawdown: list[ComparePoint] = []
    peak = 0.0
    worst = 0.0
    for d, v in shown:
        rebased = REBASE * v / base
        peak = max(peak, rebased)
        fall = rebased / peak - 1.0
        worst = min(worst, fall)
        points.append(ComparePoint(time=d, value=rebased))
        drawdown.append(ComparePoint(time=d, value=fall))
    sharpe = [
        ComparePoint(time=d, value=v)
        for d, raw in rolling_sharpe([d for d, _ in rows], [v for _, v in rows], window, periods)
        if d >= start and (v := finite(raw)) is not None
    ]
    return CompareSeriesView(
        ticker=ticker,
        points=points,
        drawdown=drawdown,
        rolling_sharpe=sharpe,
        total_return=finite(points[-1].value / REBASE - 1.0) if len(points) > 1 else None,
        max_drawdown=worst if len(points) > 1 else None,
        sharpe=sharpe[-1].value if sharpe else None,
        periods_per_year=periods,
    )


def _reason_text(raw: str | None) -> str | None:
    try:
        text = json.loads(raw or "{}").get("text")
    except (ValueError, AttributeError):
        return None
    return str(text) if text else None

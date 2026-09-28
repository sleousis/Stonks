"""LeaderboardService: strategies side by side, and one tear sheet each
(roadmap 13.6).

The leaderboard ranks every registered strategy by its risk-adjusted paper
result: the model book the tick keeps for it (``shadow_portfolio_snapshots``)
gives the value curve, and from it the return, Sharpe, Sortino and worst
drawdown. Next to it: the model book's trades, the orders it placed in real
books, how many survival tests passed, and the go-live gate's verdict.

A tear sheet is one strategy on one page: the same figures, the value curve,
monthly returns, recent model-book trades, the survival verdicts from its
lab run, the go-live report and the status history. It reads stored data
only; a fresh backtest stays in the lab.
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from datetime import date, datetime
from typing import Literal

from pydantic import BaseModel

from stonks.app.context import AppContext
from stonks.app.errors import NotFoundError
from stonks.app.golive import GoLiveReport, GoLiveService
from stonks.app.operations import PnlRowView, ShadowDecisionView
from stonks.app.serialize import finite
from stonks.app.strategies import StatusChangeView, StrategyDetail, StrategyService
from stonks.app.strategy_names import StrategyNamed
from stonks.backtest import metrics
from stonks.insights.models import MonthlyReturn
from stonks.logging import get_logger
from stonks.production.pnl import PnlRow, load_pnl
from stonks.registry.store import StatusChange
from stonks.store.state import SqliteState

_log = get_logger("stonks.app.leaderboard")

PERIODS_PER_YEAR = 252
SortKey = Literal["sharpe", "return", "drawdown", "trades"]


class PaperFigures(BaseModel):
    """The model book's result. Every figure is null without two days."""

    days: int
    first_day: date | None
    latest_day: date | None
    latest_value: float | None
    total_return: float | None
    cagr: float | None
    sharpe: float | None
    sortino: float | None
    max_drawdown: float | None
    volatility: float | None
    #: Filled model-book trades.
    trades: int


class LeaderboardRow(StrategyNamed):
    rank: int
    strategy_id: str
    class_path: str
    status: str
    paper: PaperFigures
    #: Filled orders this strategy placed in real (paper or live) books.
    book_trades: int
    #: When it last went live; null unless it is live now.
    live_since: datetime | None
    survival_passed: int
    survival_total: int
    #: The go-live gate's verdict; null for a stopped strategy or when the
    #: check could not run.
    golive_passed: bool | None


class LeaderboardView(BaseModel):
    rows: list[LeaderboardRow]
    sort: SortKey
    #: Days of history behind the figures (the longest model book).
    as_of: date | None


class TearSheetView(BaseModel):
    strategy: StrategyDetail
    paper: PaperFigures
    curve: list[PnlRowView]
    monthly_returns: list[MonthlyReturn]
    recent_trades: list[ShadowDecisionView]
    book_trades: int
    live_since: datetime | None
    golive: GoLiveReport | None
    status_history: list[StatusChangeView]


def paper_figures(rows: Sequence[PnlRow], trades: int) -> PaperFigures:
    """Figures of a daily value curve (``load_pnl`` rows)."""
    if len(rows) < 2:
        last = rows[-1] if rows else None
        return PaperFigures(
            days=len(rows),
            first_day=rows[0].day if rows else None,
            latest_day=last.day if last else None,
            latest_value=finite(last.total_value) if last else None,
            total_return=None,
            cagr=None,
            sharpe=None,
            sortino=None,
            max_drawdown=None,
            volatility=None,
            trades=trades,
        )
    curve = [r.total_value for r in rows]
    returns = [r.daily_return for r in rows[1:] if r.daily_return is not None]
    years = metrics.years_spanned([r.day for r in rows])
    total = curve[-1] / curve[0] - 1.0 if curve[0] > 0 else None
    vol = float(_std(returns)) * math.sqrt(PERIODS_PER_YEAR) if len(returns) >= 2 else None
    return PaperFigures(
        days=len(rows),
        first_day=rows[0].day,
        latest_day=rows[-1].day,
        latest_value=finite(curve[-1]),
        total_return=finite(total),
        cagr=finite(metrics.cagr(curve[0], curve[-1], years)) if years > 0 else None,
        sharpe=finite(metrics.sharpe(returns, PERIODS_PER_YEAR)) if len(returns) >= 2 else None,
        sortino=finite(metrics.sortino(returns, PERIODS_PER_YEAR)) if returns else None,
        max_drawdown=finite(min(r.drawdown for r in rows)),
        volatility=finite(vol),
        trades=trades,
    )


def monthly_returns(rows: Sequence[PnlRow]) -> list[MonthlyReturn]:
    """Month-end over previous month-end value (the first month from its
    first day), oldest first."""
    ends: dict[str, float] = {}
    starts: dict[str, float] = {}
    for r in rows:
        key = r.day.strftime("%Y-%m")
        starts.setdefault(key, r.total_value)
        ends[key] = r.total_value
    out: list[MonthlyReturn] = []
    prev: float | None = None
    for key in sorted(ends):
        base = prev if prev is not None else starts[key]
        out.append(
            MonthlyReturn(month=key, value=finite(ends[key] / base - 1.0) if base > 0 else None)
        )
        prev = ends[key]
    return out


def _std(values: Sequence[float]) -> float:
    n = len(values)
    mean = sum(values) / n
    return math.sqrt(sum((v - mean) ** 2 for v in values) / (n - 1))


def _sort_value(row: LeaderboardRow, key: SortKey) -> float:
    p = row.paper
    value = {
        "sharpe": p.sharpe,
        "return": p.total_return,
        "drawdown": p.max_drawdown,  # closer to 0 is better
        "trades": float(p.trades + row.book_trades),
    }[key]
    return -math.inf if value is None else value


class LeaderboardService:
    def __init__(self, context: AppContext, strategies: StrategyService) -> None:
        self._ctx = context
        self._strategies = strategies
        self._golive = GoLiveService(context)

    def leaderboard(
        self, *, sort: SortKey = "sharpe", include_retired: bool = False
    ) -> LeaderboardView:
        with self._ctx.registry() as registry:
            handles = [h for h in registry.list_all() if include_retired or h.status != "retired"]
            reports = {h.id: registry.get_reports(h.id) for h in handles}
            histories = {h.id: registry.status_history(h.id) for h in handles}
        rows: list[LeaderboardRow] = []
        latest: date | None = None
        with self._ctx.state() as state:
            model_trades = _counts(
                state,
                "SELECT strategy_id, COUNT(*) AS n FROM shadow_decisions"
                " WHERE status = 'filled' GROUP BY strategy_id",
            )
            book_trades = _counts(
                state,
                "SELECT strategy_id, COUNT(*) AS n FROM orders WHERE status = 'filled'"
                " AND strategy_id IS NOT NULL GROUP BY strategy_id",
            )
            for h in handles:
                figures = paper_figures(
                    load_pnl(state, strategy_id=h.id), model_trades.get(h.id, 0)
                )
                if figures.latest_day and (latest is None or figures.latest_day > latest):
                    latest = figures.latest_day
                reps = reports[h.id]
                rows.append(
                    LeaderboardRow(
                        rank=0,
                        strategy_id=h.id,
                        class_path=h.class_path,
                        status=h.status,
                        paper=figures,
                        book_trades=book_trades.get(h.id, 0),
                        live_since=_live_since(h.status, histories[h.id]),
                        survival_passed=sum(1 for r in reps if r.passed),
                        survival_total=len(reps),
                        golive_passed=None,
                    )
                )
        for row in rows:
            if row.status != "retired":
                row.golive_passed = self._golive_passed(row.strategy_id)
        rows.sort(key=lambda r: (-_sort_value(r, sort), r.strategy_id))
        for i, row in enumerate(rows, start=1):
            row.rank = i
        return LeaderboardView(rows=rows, sort=sort, as_of=latest)

    def tear_sheet(self, strategy_id: str) -> TearSheetView:
        detail = self._strategies.get(strategy_id)  # NotFoundError for an unknown id
        with self._ctx.registry() as registry:
            history = registry.status_history(strategy_id)
        with self._ctx.state() as state:
            pnl = load_pnl(state, strategy_id=strategy_id)
            trades = int(
                state.sql(
                    "SELECT COUNT(*) FROM shadow_decisions WHERE strategy_id = ?"
                    " AND status = 'filled'",
                    [strategy_id],
                )[0][0]
            )
            book = int(
                state.sql(
                    "SELECT COUNT(*) FROM orders WHERE strategy_id = ? AND status = 'filled'",
                    [strategy_id],
                )[0][0]
            )
            recent = state.sql(
                "SELECT * FROM shadow_decisions WHERE strategy_id = ?"
                " ORDER BY as_of DESC, id DESC LIMIT 25",
                [strategy_id],
            )
        golive = None
        if detail.status != "retired":
            try:
                golive = self._golive.check(strategy_id)
            except NotFoundError:
                raise
            except Exception as exc:  # a gate that cannot run leaves the sheet readable
                _log.warning(
                    "tearsheet.golive_failed", strategy_id=strategy_id, error=type(exc).__name__
                )
        return TearSheetView(
            strategy=detail,
            paper=paper_figures(pnl, trades),
            curve=[_row_view(r) for r in pnl],
            monthly_returns=monthly_returns(pnl),
            recent_trades=[_decision_view(dict(r)) for r in recent],
            book_trades=book,
            live_since=_live_since(detail.status, history),
            golive=golive,
            status_history=detail.status_history,
        )

    def _golive_passed(self, strategy_id: str) -> bool | None:
        try:
            return self._golive.check(strategy_id).passed
        except Exception as exc:  # one broken strategy never blanks the board
            _log.warning(
                "leaderboard.golive_failed", strategy_id=strategy_id, error=type(exc).__name__
            )
            return None


def _counts(state: SqliteState, query: str) -> dict[str, int]:
    return {r["strategy_id"]: int(r["n"]) for r in state.sql(query)}


def _live_since(
    status: str, history: Sequence[StatusChange] | Sequence[StatusChangeView]
) -> datetime | None:
    if status != "active":
        return None
    for change in reversed(list(history)):
        if change.to_status == "active":
            return datetime.fromisoformat(change.created_at)
    return None


def _row_view(r: PnlRow) -> PnlRowView:
    return PnlRowView(
        day=r.day,
        total_value=r.total_value,
        daily_change=finite(r.daily_change),
        daily_return=finite(r.daily_return),
        days_elapsed=r.days_elapsed,
        cumulative_return=finite(r.cumulative_return),
        drawdown=r.drawdown,
    )


def _decision_view(r: dict[str, object]) -> ShadowDecisionView:
    return ShadowDecisionView.model_validate(r)

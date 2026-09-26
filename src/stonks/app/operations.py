"""OperationsService — read-only views of the production layer: the risk
policy, the full health report and real / shadow P&L."""

from __future__ import annotations

from collections.abc import Sequence
from datetime import date, datetime
from typing import Any

from pydantic import BaseModel

from stonks.app.context import AppContext
from stonks.app.errors import NotFoundError
from stonks.app.pagination import Page
from stonks.app.serialize import finite
from stonks.config import HealthConfig, RiskPolicy
from stonks.production.health import check_health
from stonks.production.pnl import PnlRow, load_pnl


class PnlRowView(BaseModel):
    day: date
    total_value: float
    #: ``None`` on the first row and across gaps longer than a long weekend.
    daily_change: float | None
    daily_return: float | None
    #: Calendar days since the previous row.
    days_elapsed: int | None = None
    cumulative_return: float | None
    #: <= 0, relative to the running peak since inception.
    drawdown: float


class PnlSeries(BaseModel):
    """One row per day. ``strategy_id`` is ``None`` for the real portfolio
    and a shadow strategy's id for its virtual portfolio."""

    strategy_id: str | None
    rows: list[PnlRowView]


class ShadowPnlSummary(BaseModel):
    strategy_id: str
    #: Registry status now (a promoted strategy keeps its shadow history).
    status: str | None
    days: int
    first_day: date | None
    latest_day: date | None
    total_value: float | None
    cumulative_return: float | None
    max_drawdown: float | None


class ShadowDecisionView(BaseModel):
    id: int
    tick_id: str
    strategy_id: str
    as_of: date
    ticker: str
    side: str
    #: Filled quantity, or the proposed quantity when rejected.
    quantity: float
    price: float | None
    status: str
    created_at: datetime


class HealthCheckView(BaseModel):
    name: str
    ok: bool
    detail: str


class HealthReportView(BaseModel):
    healthy: bool
    checked_at: datetime
    checks: list[HealthCheckView]
    #: The ``[production.health]`` limits the checks were judged against.
    thresholds: HealthConfig


class OperationsService:
    def __init__(self, context: AppContext) -> None:
        self._ctx = context

    # ---- risk --------------------------------------------------------------

    def risk_policy(self) -> RiskPolicy:
        return self._ctx.settings.production.risk.model_copy(deep=True)

    # ---- health ------------------------------------------------------------

    def health_report(self, tickers: Sequence[str] | None = None) -> HealthReportView:
        """Every check behind ``stonks health``; freshness covers ``tickers``
        or, by default, ``[production].universe``."""
        p = self._ctx.settings.production
        universe = list(tickers) if tickers else list(p.universe)
        with self._ctx.state() as state, self._ctx.lake() as lake:
            report = check_health(state, lake, universe, p.health)
        return HealthReportView(
            healthy=report.healthy,
            checked_at=report.checked_at,
            checks=[HealthCheckView(name=c.name, ok=c.ok, detail=c.detail) for c in report.checks],
            thresholds=p.health.model_copy(),
        )

    # ---- P&L ---------------------------------------------------------------

    def pnl(self, since: date | None = None) -> PnlSeries:
        with self._ctx.state() as state:
            rows = load_pnl(state, since=since)
        return PnlSeries(strategy_id=None, rows=[_pnl_view(r) for r in rows])

    def shadow_pnl(self, strategy_id: str, since: date | None = None) -> PnlSeries:
        with self._ctx.state() as state:
            known = state.sql("SELECT 1 FROM strategies WHERE id = ?", [strategy_id])
            if not known:
                raise NotFoundError(f"no strategy with id {strategy_id!r}")
            rows = load_pnl(state, since=since, strategy_id=strategy_id)
        return PnlSeries(strategy_id=strategy_id, rows=[_pnl_view(r) for r in rows])

    def shadow_pnl_summaries(
        self, *, since: date | None = None, limit: int, offset: int
    ) -> Page[ShadowPnlSummary]:
        """Per strategy with a virtual portfolio: its latest value, return
        since inception and worst drawdown (within ``since``)."""
        with self._ctx.state() as state:
            ids_rows = state.sql(
                "SELECT DISTINCT strategy_id FROM shadow_portfolio_snapshots ORDER BY strategy_id"
            )
            ids = [r["strategy_id"] for r in ids_rows]
            page_ids = ids[offset : offset + limit]
            statuses = _statuses(state, page_ids)
            items = []
            for sid in page_ids:
                rows = load_pnl(state, since=since, strategy_id=sid)
                last = rows[-1] if rows else None
                items.append(
                    ShadowPnlSummary(
                        strategy_id=sid,
                        status=statuses.get(sid),
                        days=len(rows),
                        first_day=rows[0].day if rows else None,
                        latest_day=last.day if last else None,
                        total_value=finite(last.total_value) if last else None,
                        cumulative_return=finite(last.cumulative_return) if last else None,
                        max_drawdown=finite(min(r.drawdown for r in rows)) if rows else None,
                    )
                )
        return Page[ShadowPnlSummary](items=items, total=len(ids), limit=limit, offset=offset)

    # ---- shadow decisions --------------------------------------------------

    def shadow_decisions(
        self,
        *,
        strategy_id: str | None = None,
        ticker: str | None = None,
        as_of: date | None = None,
        limit: int,
        offset: int,
    ) -> Page[ShadowDecisionView]:
        where: list[str] = []
        params: list[Any] = []
        for column, value in (
            ("strategy_id", strategy_id),
            ("ticker", ticker),
            ("as_of", as_of.isoformat() if as_of else None),
        ):
            if value is not None:
                where.append(f"{column} = ?")
                params.append(value)
        clause = f" WHERE {' AND '.join(where)}" if where else ""
        with self._ctx.state() as state:
            total = int(state.sql(f"SELECT COUNT(*) FROM shadow_decisions{clause}", params)[0][0])
            rows = state.sql(
                f"SELECT * FROM shadow_decisions{clause} ORDER BY as_of DESC, id DESC "
                "LIMIT ? OFFSET ?",
                [*params, limit, offset],
            )
        items = [
            ShadowDecisionView(
                id=r["id"],
                tick_id=r["tick_id"],
                strategy_id=r["strategy_id"],
                as_of=date.fromisoformat(r["as_of"]),
                ticker=r["ticker"],
                side=r["side"],
                quantity=float(r["quantity"]),
                price=None if r["price"] is None else float(r["price"]),
                status=r["status"],
                created_at=datetime.fromisoformat(r["created_at"]),
            )
            for r in rows
        ]
        return Page[ShadowDecisionView](items=items, total=total, limit=limit, offset=offset)


def _pnl_view(row: PnlRow) -> PnlRowView:
    return PnlRowView(
        day=row.day,
        total_value=float(row.total_value),
        daily_change=finite(row.daily_change),
        daily_return=finite(row.daily_return),
        days_elapsed=row.days_elapsed,
        cumulative_return=finite(row.cumulative_return),
        drawdown=finite(row.drawdown) or 0.0,
    )


def _statuses(state: Any, ids: list[str]) -> dict[str, str]:
    if not ids:
        return {}
    marks = ", ".join("?" for _ in ids)
    rows = state.sql(f"SELECT id, status FROM strategies WHERE id IN ({marks})", ids)
    return {r["id"]: r["status"] for r in rows}

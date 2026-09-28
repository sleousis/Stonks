"""OperationsService — read-only views of the production layer: the risk
policy, the full health report and real / shadow P&L."""

from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, date, datetime
from typing import Any

from pydantic import BaseModel

from stonks.accounts.models import DEFAULT_PORTFOLIO_ID
from stonks.app.context import AppContext
from stonks.app.errors import NotFoundError
from stonks.app.pagination import Page
from stonks.app.portfolio import DayChangeView
from stonks.app.serialize import finite
from stonks.app.strategy_names import StrategyNamed
from stonks.config import HealthConfig, RiskPolicy
from stonks.insights.flows import flows_or_missing, lake_fx_loader
from stonks.insights.returns import mwr, net_flows, twr
from stonks.production.halts import HEALTH_ACTOR, read_health, run_health
from stonks.production.pnl import PnlRow, load_pnl
from stonks.production.universe import EmptyUniverseError, production_tickers


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
    #: The portfolio's base currency (real portfolios only, roadmap 20.5).
    base_currency: str | None = None
    #: ``rows`` revalued in the base currency: cash as is, each holding at
    #: that day's close and FX rate. ``None`` for shadow books, or when a
    #: held currency has no FX rate on some day (see ``fx_missing``).
    base_rows: list[PnlRowView] | None = None
    fx_missing: list[str] = []
    #: Deposits less withdrawals inside the range of the rows (real portfolios).
    net_flows: float = 0.0
    #: Time-weighted return over the rows: deposits and withdrawals taken
    #: out, so a deposit is never profit (``cumulative_return`` is the plain
    #: change in value).
    twr: float | None = None
    #: Money-weighted return over the rows, annualized (XIRR).
    mwr: float | None = None
    #: The headline value and day change (real portfolios), the same one
    #: ``/api/insights`` carries.
    day_change: DayChangeView | None = None


class ShadowPnlSummary(StrategyNamed):
    strategy_id: str
    #: Registry status now (a promoted strategy keeps its shadow history).
    status: str | None
    days: int
    first_day: date | None
    latest_day: date | None
    total_value: float | None
    cumulative_return: float | None
    max_drawdown: float | None


class ShadowDecisionView(StrategyNamed):
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


def _health_universe(lake: Any, configured: Any, tickers: Sequence[str] | None) -> list[str]:
    """``tickers``, else ``[production].universe`` resolved on today (a
    list, or a universe id's members). A universe that resolves to nothing
    checks no freshness, so it never opens the operational halt (BE-06)."""
    try:
        return production_tickers(lake, configured, datetime.now(UTC).date(), tickers=tickers)
    except EmptyUniverseError:
        return []


class OperationsService:
    def __init__(self, context: AppContext) -> None:
        self._ctx = context

    # ---- risk --------------------------------------------------------------

    def risk_policy(self) -> RiskPolicy:
        return self._ctx.settings.production.risk.model_copy(deep=True)

    # ---- health ------------------------------------------------------------

    def health_report(self, tickers: Sequence[str] | None = None) -> HealthReportView:
        """Every check behind ``stonks health``; freshness covers ``tickers``
        or, by default, ``[production].universe``. Read-only: it lists open
        halts but never opens or clears one (TO-03)."""
        p = self._ctx.settings.production
        with self._ctx.state() as state, self._ctx.lake() as lake:
            universe = _health_universe(lake, p.universe, tickers)
            report = read_health(state, lake, universe, p.health)
        return _health_view(report, p.health)

    def run_health(
        self, tickers: Sequence[str] | None = None, *, actor: str = HEALTH_ACTOR
    ) -> HealthReportView:
        """The scheduled health job: the same checks, then the global
        operational halt is opened or cleared from them, as ``actor``.
        Callers are trusted (the scheduler, an admin through
        ``operations.run``)."""
        p = self._ctx.settings.production
        with self._ctx.state() as state, self._ctx.lake() as lake:
            universe = _health_universe(lake, p.universe, tickers)
            report = run_health(state, lake, universe, p.health, actor=actor)
        return _health_view(report, p.health)

    # ---- P&L ---------------------------------------------------------------

    def pnl(
        self, since: date | None = None, *, portfolio_id: str = DEFAULT_PORTFOLIO_ID
    ) -> PnlSeries:
        """One portfolio's daily P&L. Callers resolve ``portfolio_id``
        through ``PortfolioService.resolve`` first."""
        with self._ctx.state() as state:
            rows = load_pnl(state, since=since, portfolio_id=portfolio_id)
            snapshots, base = _snapshot_points(state, portfolio_id)
            found, flow_missing = flows_or_missing(
                state, portfolio_id, fx_loader=lake_fx_loader(self._ctx.lake)
            )
        flows = found or []
        base_rows, missing = self._base_rows(snapshots, base, since)
        if flow_missing:
            missing = sorted({*missing, flow_missing})
        points = [(r.day, r.total_value) for r in rows]
        return PnlSeries(
            strategy_id=None,
            rows=[_pnl_view(r) for r in rows],
            base_currency=base,
            base_rows=base_rows,
            fx_missing=missing,
            net_flows=net_flows(points, flows),
            twr=None if flow_missing else twr(points, flows),
            mwr=None if flow_missing else mwr(points, flows),
            day_change=DayChangeView.of(rows),
        )

    def _base_rows(
        self, snapshots: list[Any], base: str, since: date | None
    ) -> tuple[list[PnlRowView] | None, list[str]]:
        """The daily P&L in ``base`` (see :mod:`stonks.fx.valuation`)."""
        from stonks.fx import FxRates, load_fx_rates
        from stonks.fx.valuation import CloseSeries, values_in_base
        from stonks.production.pnl import daily_pnl

        tickers = sorted({t for s in snapshots for t in s.positions})
        closes: dict[str, list[tuple[date, float]]] = {}
        currencies: dict[str, str] = {}
        fx = FxRates([])
        if tickers:
            with self._ctx.lake() as lake:
                cur = lake.sql(
                    "SELECT id, currency FROM instruments WHERE id = ANY(?)"
                    " AND currency IS NOT NULL",
                    [tickers],
                )
                currencies = {
                    str(i): str(c) for i, c in zip(cur["id"], cur["currency"], strict=True)
                }
                if any(c != base for c in currencies.values()):
                    df = lake.sql(
                        "SELECT ticker, date, close FROM prices WHERE ticker = ANY(?)"
                        " AND close IS NOT NULL",
                        [tickers],
                    )
                    for ticker, when, close in zip(
                        df["ticker"], df["date"], df["close"], strict=True
                    ):
                        day = when.date() if isinstance(when, datetime) else when
                        closes.setdefault(str(ticker), []).append((day, float(close)))
                    fx = load_fx_rates(lake, {*currencies.values(), base})
        points, missing = values_in_base(snapshots, CloseSeries(closes), currencies, base, fx)
        if points is None:
            return None, missing
        return [_pnl_view(r) for r in daily_pnl(points, since=since)], missing

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


def _health_view(report: Any, thresholds: HealthConfig) -> HealthReportView:
    return HealthReportView(
        healthy=report.healthy,
        checked_at=report.checked_at,
        checks=[HealthCheckView(name=c.name, ok=c.ok, detail=c.detail) for c in report.checks],
        thresholds=thresholds.model_copy(),
    )


def _snapshot_points(state: Any, portfolio_id: str) -> tuple[list[Any], str]:
    """The portfolio's last snapshot per day (as ``load_pnl`` reads them)
    with cash and holdings, and its base currency."""
    import json

    from stonks.fx.valuation import SnapshotPoint
    from stonks.production.ledger import ledger_filter
    from stonks.production.pnl import _snapshot_day

    where, params = ledger_filter(state, "portfolio_snapshots", portfolio_id)
    rows = state.sql(
        "SELECT as_of, taken_at, cash, positions_json, total_value FROM portfolio_snapshots"
        f" WHERE {where} ORDER BY id",
        params,
    )
    by_day: dict[date, Any] = {}
    for r in rows:
        day = _snapshot_day(r["as_of"], r["taken_at"])
        by_day[day] = SnapshotPoint(
            day=day,
            cash=float(r["cash"]),
            positions=json.loads(r["positions_json"] or "{}"),
            total_value=float(r["total_value"]),
        )
    found = state.sql("SELECT base_currency FROM portfolios WHERE id = ?", [portfolio_id])
    base = str(found[0]["base_currency"]).upper() if found else "USD"
    return [by_day[d] for d in sorted(by_day)], base


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

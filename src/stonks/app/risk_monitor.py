"""RiskMonitorService: live VaR, ES, violation scores and alpha decay
(BL-47, roadmap 9.5.4) for the API and MCP. The math and the rows live in
:mod:`stonks.production.risk_metrics`; this module reads one portfolio,
resolved by the caller (``PortfolioService.resolve`` in the API, which
answers 404 for another user's book)."""

from __future__ import annotations

from datetime import date
from typing import Any

from pydantic import BaseModel

from stonks.app.context import AppContext
from stonks.app.pagination import Page
from stonks.production.risk_metrics import (
    PORTFOLIO_BOOK,
    RiskMonitorSettings,
    RiskSnapshot,
    latest_snapshots,
    list_snapshots,
    risk_snapshots_enabled,
)


class RiskSnapshotView(BaseModel):
    """One book on one day. Figures are fractions of ``value``; a loss is
    positive. ``strategy_id`` is null for the whole portfolio."""

    portfolio_id: str
    strategy_id: str | None
    as_of: date
    tick_id: str | None
    #: Portfolio: cash plus positions. Sleeve: gross exposure.
    value: float
    exposures: dict[str, float]
    sigma: float | None
    var_95: float | None
    var_99: float | None
    es_95: float | None
    es_99: float | None
    #: Daily returns behind the forecast.
    observations: int
    #: Yesterday's exposures times today's returns, over yesterday's value.
    realized_return: float | None
    pnl: float | None
    violation_95: bool | None
    violation_99: bool | None
    #: Scored days in the rolling window.
    window_days: int
    violations_95: int
    violations_99: int
    #: Observed over expected violations; 1.0 is a well-calibrated model.
    violation_ratio_95: float | None
    violation_ratio_99: float | None
    kupiec_p_95: float | None
    kupiec_p_99: float | None
    #: Rolling annualised IR of the sleeve (short and long window).
    ir_short: float | None
    ir_long: float | None
    #: What the backtest promised.
    expected_ir: float | None
    decay_days: int | None
    decayed: bool
    decay_reason: str | None
    #: The violation ratio is judged and outside the band.
    ratio_out_of_band: bool

    @classmethod
    def of(cls, snap: RiskSnapshot, settings: RiskMonitorSettings) -> RiskSnapshotView:
        data: dict[str, Any] = snap.as_dict()
        data.pop("created_at", None)
        return cls(**data, ratio_out_of_band=snap.ratio_out_of_band(settings))


class RiskSummaryView(BaseModel):
    portfolio_id: str
    #: The latest snapshot day, null before the first real tick.
    as_of: date | None
    portfolio: RiskSnapshotView | None
    strategies: list[RiskSnapshotView]


class RiskMonitorService:
    def __init__(self, context: AppContext) -> None:
        self._context = context
        self._settings = RiskMonitorSettings()

    def latest(self, portfolio_id: str) -> RiskSummaryView:
        """The portfolio and each strategy sleeve on the latest day."""
        with self._context.state() as state:
            snaps = latest_snapshots(state, portfolio_id) if risk_snapshots_enabled(state) else []
        views = [RiskSnapshotView.of(s, self._settings) for s in snaps]
        whole = next((v for v in views if v.strategy_id is None), None)
        return RiskSummaryView(
            portfolio_id=portfolio_id,
            as_of=snaps[0].as_of if snaps else None,
            portfolio=whole,
            strategies=[v for v in views if v.strategy_id is not None],
        )

    def history(
        self,
        portfolio_id: str,
        *,
        strategy_id: str | None = None,
        since: date | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> Page[RiskSnapshotView]:
        """The whole portfolio's rows (default) or one strategy's sleeve,
        newest first."""
        book = strategy_id if strategy_id is not None else PORTFOLIO_BOOK
        with self._context.state() as state:
            if not risk_snapshots_enabled(state):
                return Page[RiskSnapshotView](items=[], total=0, limit=limit, offset=offset)
            snaps, total = list_snapshots(
                state, portfolio_id, strategy_id=book, since=since, limit=limit, offset=offset
            )
        return Page[RiskSnapshotView](
            items=[RiskSnapshotView.of(s, self._settings) for s in snaps],
            total=total,
            limit=limit,
            offset=offset,
        )

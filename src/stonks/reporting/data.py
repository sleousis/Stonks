"""Gather everything the static report shows from ``SqliteState``.

Portfolio and paper P&L come only from ``stonks.production.pnl`` /
``stonks.production.golive`` so the report and the CLI agree on the numbers.
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from typing import Any

from stonks.config import GoLivePolicy
from stonks.core.protocols import SurvivalReport
from stonks.production.golive import GoLiveCheck, PaperPeriod, gate_checks, load_paper_period
from stonks.production.pnl import PnlRow, load_pnl
from stonks.registry.store import StrategyHandle, StrategyRegistry
from stonks.reporting.tearsheet import TearSheet
from stonks.store.state import SqliteState

RECENT_LIMIT = 50


@dataclass(frozen=True)
class Positions:
    taken_at: str
    cash: float
    total_value: float
    quantities: dict[str, float]


@dataclass(frozen=True)
class StrategyPanel:
    handle: StrategyHandle
    reports: list[SurvivalReport]
    paper: PaperPeriod
    gate: list[GoLiveCheck]

    @property
    def expected_curve(self) -> list[tuple[date, float]]:
        """The backtest expectation compounded from the paper period's first
        value, day by day; empty without a finite expectation."""
        cagr = self.paper.expected_cagr
        rows = self.paper.rows
        if cagr is None or not rows or rows[0].total_value <= 0:
            return []
        start_day, start_value = rows[0].day, rows[0].total_value
        curve = []
        for r in rows:
            try:
                value = start_value * (1.0 + cagr) ** ((r.day - start_day).days / 365.25)
            except OverflowError:
                return []
            curve.append((r.day, value))
        return curve


@dataclass(frozen=True)
class ReportData:
    generated_at: datetime
    since: date | None
    portfolio: list[PnlRow]
    positions: Positions | None
    orders: list[dict[str, Any]]
    fills: list[dict[str, Any]]
    strategies: list[StrategyPanel]
    unknown_strategy_ids: list[str] = field(default_factory=list)
    policy: GoLivePolicy = field(default_factory=GoLivePolicy)
    #: Backtests to show as tear sheets (strategy vs benchmark).
    tear_sheets: list[TearSheet] = field(default_factory=list)


def build_report(
    state: SqliteState,
    registry: StrategyRegistry,
    policy: GoLivePolicy,
    strategy_ids: Sequence[str] | None = None,
    since: date | None = None,
    now: datetime | None = None,
    tear_sheets: Sequence[TearSheet] = (),
) -> ReportData:
    handles = registry.list_all()
    unknown: list[str] = []
    if strategy_ids:
        by_id = {h.id: h for h in handles}
        unknown = [s for s in strategy_ids if s not in by_id]
        handles = [by_id[s] for s in dict.fromkeys(strategy_ids) if s in by_id]

    panels = []
    for h in handles:
        paper = load_paper_period(state, registry, h.id, since=since)
        panels.append(
            StrategyPanel(
                handle=h, reports=paper.reports, paper=paper, gate=gate_checks(paper, policy)
            )
        )

    return ReportData(
        generated_at=now or datetime.now(UTC),
        since=since,
        portfolio=load_pnl(state, since=since),
        positions=_latest_positions(state),
        orders=_recent_orders(state, since),
        fills=_recent_fills(state, since),
        strategies=panels,
        unknown_strategy_ids=unknown,
        policy=policy,
        tear_sheets=list(tear_sheets),
    )


def _latest_positions(state: SqliteState) -> Positions | None:
    rows = state.sql(
        "SELECT taken_at, cash, total_value, positions_json FROM portfolio_snapshots "
        "ORDER BY id DESC LIMIT 1"
    )
    if not rows:
        return None
    r = rows[0]
    try:
        raw = json.loads(r["positions_json"])
    except (TypeError, ValueError):
        raw = {}
    quantities = {str(k): float(v) for k, v in raw.items()} if isinstance(raw, dict) else {}
    return Positions(
        taken_at=str(r["taken_at"]),
        cash=float(r["cash"]),
        total_value=float(r["total_value"]),
        quantities=quantities,
    )


def _recent_orders(state: SqliteState, since: date | None) -> list[dict[str, Any]]:
    rows = state.sql(
        "SELECT created_at, strategy_id, ticker, side, quantity, order_type, status "
        "FROM orders WHERE substr(created_at, 1, 10) >= ? "
        "ORDER BY created_at DESC, client_id DESC LIMIT ?",
        [since.isoformat() if since else "", RECENT_LIMIT],
    )
    return [dict(r) for r in rows]


def _recent_fills(state: SqliteState, since: date | None) -> list[dict[str, Any]]:
    rows = state.sql(
        "SELECT f.filled_at, o.strategy_id, f.ticker, o.side, f.quantity, f.price, f.fee "
        "FROM fills f LEFT JOIN orders o ON o.client_id = f.order_client_id "
        "WHERE substr(f.filled_at, 1, 10) >= ? "
        "ORDER BY f.filled_at DESC, f.id DESC LIMIT ?",
        [since.isoformat() if since else "", RECENT_LIMIT],
    )
    return [dict(r) for r in rows]

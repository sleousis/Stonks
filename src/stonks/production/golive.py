"""Go-live gate (roadmap 4.3), behind ``stonks golive check <id>``.

Evaluates a strategy's paper-trading period against ``GoLivePolicy``:

- ``status``: the strategy is ``shadow`` or ``active`` (a retired one has no
  paper period);
- ``min_days``: distinct days with a paper snapshot;
- ``max_drawdown``: deepest peak-to-trough fall within the period;
- ``max_drift``: |paper return - backtest-expected return| over the period,
  where the expectation compounds the latest ``oos`` survival report's
  ``cagr_oos`` over the period's calendar span;
- ``min_trades``: filled trades during the period;
- ``survival``: every stored survival report passed (when required).

The paper period is the strategy's virtual shadow P&L while it is
``shadow``, and the real (simulated or paper-broker) portfolio P&L once it
is ``active``. The real portfolio is shared by all active strategies, so for
an active strategy the P&L checks describe the combined book; trades are
still counted per strategy. P&L is read only through
``stonks.production.pnl`` so its day grouping stays in one place.

Missing data never passes: no snapshots fails ``min_days``,
``max_drawdown`` and ``max_drift``; no backtest expectation fails
``max_drift``; no survival reports fails ``survival``.

The gate only reports. It never changes a strategy's status; promotion
stays a human action (``stonks registry promote``).
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import date
from typing import Literal

from stonks.config import GoLivePolicy
from stonks.core.protocols import SurvivalReport
from stonks.production.pnl import PnlRow, daily_pnl, load_pnl
from stonks.registry.store import StrategyRegistry
from stonks.store.state import SqliteState

PaperSource = Literal["shadow", "portfolio", "none"]

_DAYS_PER_YEAR = 365.25  # matches BacktestReport's CAGR annualization


@dataclass(frozen=True)
class PaperPeriod:
    strategy_id: str
    status: str
    source: PaperSource
    # P&L rows re-based to the period's first day (returns and drawdown are
    # measured within the period, not from portfolio inception).
    rows: list[PnlRow]
    trades: int
    reports: list[SurvivalReport]

    @property
    def days(self) -> int:
        return len(self.rows)

    @property
    def period_return(self) -> float | None:
        return self.rows[-1].cumulative_return if self.rows else None

    @property
    def max_drawdown(self) -> float | None:
        """Deepest drawdown as a positive fraction; None without data."""
        if not self.rows:
            return None
        return -min(r.drawdown for r in self.rows)

    @property
    def expected_cagr(self) -> float | None:
        for report in reversed(self.reports):
            if report.test_id == "oos":
                value = report.metrics.get("cagr_oos")
                if isinstance(value, int | float) and math.isfinite(value) and value > -1.0:
                    return float(value)
                return None
        return None

    @property
    def expected_return(self) -> float | None:
        """Backtest-expected return over the period's calendar span."""
        cagr = self.expected_cagr
        if cagr is None or not self.rows:
            return None
        years = (self.rows[-1].day - self.rows[0].day).days / _DAYS_PER_YEAR
        try:
            expected = (1.0 + cagr) ** years - 1.0
        except OverflowError:
            return None
        return expected if math.isfinite(expected) else None

    @property
    def drift(self) -> float | None:
        """Paper return minus expected return; None when either is unknown."""
        live, expected = self.period_return, self.expected_return
        if live is None or expected is None:
            return None
        return live - expected


@dataclass(frozen=True)
class GoLiveCheck:
    name: str
    passed: bool
    value: float | None
    limit: float | None
    detail: str


@dataclass(frozen=True)
class GoLiveReport:
    strategy_id: str
    status: str
    source: PaperSource
    checks: list[GoLiveCheck]

    @property
    def passed(self) -> bool:
        return bool(self.checks) and all(c.passed for c in self.checks)

    @property
    def failures(self) -> list[GoLiveCheck]:
        return [c for c in self.checks if not c.passed]


def load_paper_period(
    state: SqliteState,
    registry: StrategyRegistry,
    strategy_id: str,
    since: date | None = None,
) -> PaperPeriod:
    """The strategy's paper period. Raises ``KeyError`` for an unknown id."""
    handle = next((h for h in registry.list_all() if h.id == strategy_id), None)
    if handle is None:
        raise KeyError(strategy_id)
    reports = registry.get_reports(strategy_id)

    source: PaperSource
    if handle.status == "shadow":
        source = "shadow"
        raw = load_pnl(state, since=since, strategy_id=strategy_id)
        trades = _shadow_trades(state, strategy_id, since)
    elif handle.status == "active":
        source = "portfolio"
        raw = load_pnl(state, since=since)
        trades = _real_trades(state, strategy_id, since)
    else:
        source, raw, trades = "none", [], 0

    rows = daily_pnl([(r.day, r.total_value) for r in raw])
    return PaperPeriod(
        strategy_id=strategy_id,
        status=handle.status,
        source=source,
        rows=rows,
        trades=trades,
        reports=reports,
    )


def evaluate_golive(
    state: SqliteState,
    registry: StrategyRegistry,
    strategy_id: str,
    policy: GoLivePolicy,
    since: date | None = None,
) -> GoLiveReport:
    period = load_paper_period(state, registry, strategy_id, since=since)
    return GoLiveReport(
        strategy_id=strategy_id,
        status=period.status,
        source=period.source,
        checks=gate_checks(period, policy),
    )


def gate_checks(period: PaperPeriod, policy: GoLivePolicy) -> list[GoLiveCheck]:
    checks = [_status_check(period)]
    checks.append(
        GoLiveCheck(
            name="min_days",
            passed=period.days >= policy.min_days,
            value=period.days,
            limit=policy.min_days,
            detail=f"{period.days} paper day(s), need >= {policy.min_days}",
        )
    )

    dd = period.max_drawdown
    checks.append(
        GoLiveCheck(
            name="max_drawdown",
            passed=dd is not None and dd <= policy.max_drawdown,
            value=dd,
            limit=policy.max_drawdown,
            detail=(
                "no paper snapshots"
                if dd is None
                else f"max drawdown {dd:.2%}, limit {policy.max_drawdown:.2%}"
            ),
        )
    )

    drift = period.drift
    if drift is not None:
        drift_detail = (
            f"paper {period.period_return:+.2%} vs backtest {period.expected_return:+.2%} "
            f"(gap {drift:+.2%}, limit ±{policy.max_drift:.2%})"
        )
    elif not period.rows:
        drift_detail = "no paper snapshots"
    elif period.expected_return is None:
        drift_detail = "no finite backtest expectation (oos cagr_oos)"
    else:
        drift_detail = "paper return undefined (zero starting value)"
    checks.append(
        GoLiveCheck(
            name="max_drift",
            passed=drift is not None and abs(drift) <= policy.max_drift,
            value=drift,
            limit=policy.max_drift,
            detail=drift_detail,
        )
    )

    checks.append(
        GoLiveCheck(
            name="min_trades",
            passed=period.trades >= policy.min_trades,
            value=period.trades,
            limit=policy.min_trades,
            detail=f"{period.trades} filled trade(s), need >= {policy.min_trades}",
        )
    )

    passed_n = sum(1 for r in period.reports if r.passed)
    total_n = len(period.reports)
    failed_ids = [r.test_id for r in period.reports if not r.passed]
    if policy.require_all_survival_passed:
        survival_ok = total_n > 0 and passed_n == total_n
        survival_detail = (
            "no survival reports"
            if total_n == 0
            else f"{passed_n}/{total_n} passed"
            + (f"; failed: {', '.join(failed_ids)}" if failed_ids else "")
        )
    else:
        survival_ok = True
        survival_detail = f"{passed_n}/{total_n} passed (not required)"
    checks.append(
        GoLiveCheck(
            name="survival",
            passed=survival_ok,
            value=passed_n,
            limit=total_n,
            detail=survival_detail,
        )
    )
    return checks


def _status_check(period: PaperPeriod) -> GoLiveCheck:
    ok = period.source != "none"
    detail = (
        f"{period.status}: paper period from {period.source} P&L"
        if ok
        else f"{period.status}: no paper period"
    )
    return GoLiveCheck(name="status", passed=ok, value=None, limit=None, detail=detail)


def _shadow_trades(state: SqliteState, strategy_id: str, since: date | None) -> int:
    rows = state.sql(
        "SELECT COUNT(*) AS n FROM shadow_decisions "
        "WHERE strategy_id = ? AND status = 'filled' AND as_of >= ?",
        [strategy_id, since.isoformat() if since else ""],
    )
    return int(rows[0]["n"])


def _real_trades(state: SqliteState, strategy_id: str, since: date | None) -> int:
    rows = state.sql(
        "SELECT COUNT(*) AS n FROM fills f JOIN orders o ON o.client_id = f.order_client_id "
        "WHERE o.strategy_id = ? AND substr(f.filled_at, 1, 10) >= ?",
        [strategy_id, since.isoformat() if since else ""],
    )
    return int(rows[0]["n"])

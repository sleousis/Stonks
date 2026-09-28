"""The swap check (roadmap 22.6): the go-live style gate a candidate model
version passes before it replaces the live one.

It reads the version model books (``production.version_books``), which
start on the same day for the candidate and the live version, and checks:

- ``candidate``: the version is a candidate and the strategy is not retired;
- ``min_days``: the candidate book has at least ``min_days`` snapshots;
- ``max_drawdown``: the candidate book's deepest fall is within the limit;
- ``vs_live``: over the candidate's days, its return trails the live
  version's by at most ``max_underperformance``.

Missing data never passes. The check only reports; the swap itself goes
through ``ModelVersionRegistry.swap``, which stores the report.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from stonks.lifecycle.settings import SwapPolicy
from stonks.production.pnl import daily_pnl
from stonks.production.version_books import version_book_curve
from stonks.registry.versions import ModelVersionRegistry
from stonks.store.state import SqliteState


@dataclass(frozen=True)
class SwapCheck:
    name: str
    passed: bool
    value: float | None
    limit: float | None
    detail: str

    def as_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "passed": self.passed,
            "value": self.value,
            "limit": self.limit,
            "detail": self.detail,
        }


@dataclass(frozen=True)
class SwapReport:
    strategy_id: str
    version: int
    live_version: int
    checks: list[SwapCheck] = field(default_factory=list)
    days: int = 0
    candidate_return: float | None = None
    live_return: float | None = None
    candidate_drawdown: float | None = None

    @property
    def passed(self) -> bool:
        return bool(self.checks) and all(c.passed for c in self.checks)

    def as_dict(self) -> dict[str, Any]:
        return {
            "strategy_id": self.strategy_id,
            "version": self.version,
            "live_version": self.live_version,
            "passed": self.passed,
            "days": self.days,
            "candidate_return": self.candidate_return,
            "live_return": self.live_return,
            "candidate_drawdown": self.candidate_drawdown,
            "checks": [c.as_dict() for c in self.checks],
        }


def evaluate_swap(
    state: SqliteState,
    versions: ModelVersionRegistry,
    strategy_id: str,
    version: int,
    policy: SwapPolicy,
) -> SwapReport:
    """Every check for swapping ``version`` in. ``KeyError`` for an unknown
    strategy or version."""
    target = versions.get(strategy_id, version)
    live = versions.live(strategy_id)
    (row,) = state.sql("SELECT status FROM strategies WHERE id = ?", [strategy_id])
    candidate_rows = daily_pnl(version_book_curve(state, strategy_id, version))
    since = candidate_rows[0].day if candidate_rows else None
    live_rows = (
        daily_pnl(version_book_curve(state, strategy_id, live.version, since=since))
        if since is not None
        else []
    )
    cand_return = candidate_rows[-1].cumulative_return if candidate_rows else None
    live_return = live_rows[-1].cumulative_return if live_rows else None
    drawdown = -min(r.drawdown for r in candidate_rows) if candidate_rows else None

    checks = [
        _status_check(target.status, row["status"]),
        SwapCheck(
            name="min_days",
            passed=len(candidate_rows) >= policy.min_days,
            value=float(len(candidate_rows)),
            limit=float(policy.min_days),
            detail=f"{len(candidate_rows)} model book day(s), need >= {policy.min_days}",
        ),
        SwapCheck(
            name="max_drawdown",
            passed=drawdown is not None and drawdown <= policy.max_drawdown,
            value=drawdown,
            limit=policy.max_drawdown,
            detail=(
                "no model book snapshots"
                if drawdown is None
                else f"max drawdown {drawdown:.2%}, limit {policy.max_drawdown:.2%}"
            ),
        ),
        _vs_live_check(cand_return, live_return, live.version, policy),
    ]
    return SwapReport(
        strategy_id=strategy_id,
        version=version,
        live_version=live.version,
        checks=checks,
        days=len(candidate_rows),
        candidate_return=cand_return,
        live_return=live_return,
        candidate_drawdown=drawdown,
    )


def _status_check(version_status: str, strategy_status: str) -> SwapCheck:
    ok = version_status == "candidate" and strategy_status != "retired"
    detail = (
        f"version is {version_status}, strategy is {strategy_status}"
        if not ok
        else f"candidate of a {strategy_status} strategy"
    )
    return SwapCheck(name="candidate", passed=ok, value=None, limit=None, detail=detail)


def _vs_live_check(
    candidate: float | None, live: float | None, live_version: int, policy: SwapPolicy
) -> SwapCheck:
    limit = -policy.max_underperformance
    if candidate is None or live is None:
        return SwapCheck(
            name="vs_live",
            passed=False,
            value=None,
            limit=limit,
            detail=f"no model books for the candidate and live v{live_version} over the same days",
        )
    gap = candidate - live
    return SwapCheck(
        name="vs_live",
        passed=gap >= limit,
        value=gap,
        limit=limit,
        detail=(
            f"candidate {candidate:+.2%} vs live v{live_version} {live:+.2%} "
            f"(gap {gap:+.2%}, may trail by {policy.max_underperformance:.2%})"
        ),
    )

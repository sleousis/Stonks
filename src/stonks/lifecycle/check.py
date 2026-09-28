"""The swap check (roadmap 22.6): the go-live style gate a candidate model
version passes before it replaces the live one.

It reads the version model books (``production.version_books``), which
start on the same day for the candidate and the live version, and checks:

- ``candidate``: the version is a candidate and the strategy is not retired;
- ``min_days``: the candidate book has at least ``min_days`` snapshots;
- ``max_drawdown``: the candidate book's deepest fall is within the limit;
- ``vs_live``: a paired test over the days both books have (roadmap
  23.9). The daily return gaps (candidate minus live) get a HAC t-test of
  their mean. It fails when the candidate trails with a one-sided p-value
  below ``vs_live_alpha``, or with fewer than ``min_paired_days`` pairs.
  Raw cumulative returns are reported, never judged: over a few weeks
  their gap is mostly noise.

Missing data never passes. The check only reports; the swap itself goes
through ``ModelVersionRegistry.swap``, which stores the report.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Any

import numpy as np
from scipy.stats import norm

from stonks.lifecycle.settings import SwapPolicy
from stonks.production.pnl import PnlRow, daily_pnl
from stonks.production.version_books import version_book_curve
from stonks.registry.versions import ModelVersionRegistry
from stonks.stats.hac import hac_mean_test
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
    #: Days both books have a daily return, and the HAC t-statistic of the
    #: mean daily gap (``None`` with fewer than two pairs).
    paired_days: int = 0
    gap_t_stat: float | None = None

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
            "paired_days": self.paired_days,
            "gap_t_stat": self.gap_t_stat,
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
    gaps = paired_gaps(candidate_rows, live_rows)
    vs_live, t_stat = _vs_live_check(gaps, live.version, policy)

    checks = [
        _status_check(target.status, row["status"]),
        SwapCheck(
            name="min_days",
            passed=len(candidate_rows) >= policy.min_days,
            value=float(len(candidate_rows)),
            limit=float(policy.min_days),
            detail=f"{len(candidate_rows)} test book day(s), need >= {policy.min_days}",
        ),
        SwapCheck(
            name="max_drawdown",
            passed=drawdown is not None and drawdown <= policy.max_drawdown,
            value=drawdown,
            limit=policy.max_drawdown,
            detail=(
                "no test book snapshots"
                if drawdown is None
                else f"max drawdown {drawdown:.2%}, limit {policy.max_drawdown:.2%}"
            ),
        ),
        vs_live,
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
        paired_days=len(gaps),
        gap_t_stat=t_stat,
    )


def _status_check(version_status: str, strategy_status: str) -> SwapCheck:
    ok = version_status == "candidate" and strategy_status != "retired"
    detail = (
        f"version is {version_status}, strategy is {strategy_status}"
        if not ok
        else f"candidate of a {strategy_status} strategy"
    )
    return SwapCheck(name="candidate", passed=ok, value=None, limit=None, detail=detail)


def paired_gaps(candidate: list[PnlRow], live: list[PnlRow]) -> list[float]:
    """Candidate minus live daily return on each day both books have one."""
    live_by_day = {r.day: r.daily_return for r in live if r.daily_return is not None}
    return [
        r.daily_return - live_by_day[r.day]
        for r in candidate
        if r.daily_return is not None and r.day in live_by_day
    ]


def _vs_live_check(
    gaps: list[float], live_version: int, policy: SwapPolicy
) -> tuple[SwapCheck, float | None]:
    """The paired test. ``value`` is the HAC t-statistic of the mean daily
    gap, ``limit`` the one-sided critical value it must reach."""
    limit = float(norm.ppf(policy.vs_live_alpha))
    if len(gaps) < 2:
        check = SwapCheck(
            name="vs_live",
            passed=False,
            value=None,
            limit=limit,
            detail=f"{len(gaps)} paired day(s) with the test book of the live v{live_version}",
        )
        return check, None
    test = hac_mean_test(np.asarray(gaps))
    enough = test.n >= policy.min_paired_days
    not_worse = test.p_below >= policy.vs_live_alpha
    words = (
        f"mean daily gap {test.mean:+.3%} vs live v{live_version} over {test.n} paired days "
        f"(HAC t {test.t_stat:+.2f}, p {test.p_below:.3f} that it trails)"
    )
    if not enough:
        words += f", need >= {policy.min_paired_days} paired days"
    elif not not_worse:
        words += f": trails live at p < {policy.vs_live_alpha:g}"
    t_stat = _finite_or_none(test.t_stat)
    check = SwapCheck(
        name="vs_live",
        passed=enough and not_worse,
        value=t_stat,
        limit=limit,
        detail=words,
    )
    return check, t_stat


def _finite_or_none(x: float) -> float | None:
    return float(x) if math.isfinite(x) else None

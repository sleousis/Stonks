"""Correlation-to-pool survival test (BL-47, principle P17; Tulchinsky,
Meucci): a new strategy must bring something the active pool lacks.

It backtests the candidate and every active strategy on the validation
window (or the full window) and correlates their daily returns. It fails
when the correlation with any pool member is above ``max_correlation``
(0.7), unless the candidate's IR (the annualised Sharpe of its returns) is
at least ``ir_margin`` (10%) better than that member's. A pool member with
the candidate's own class and params is the candidate itself and is left
out.

The pool is the registry's active list: the ``registry`` handed to the
constructor, else the one ``load_settings()`` points at. An empty pool
passes: there is nothing to be redundant with. A member that fails to load
or backtest is skipped and named in the notes. Pool backtests run one after
another (the pool is a handful of strategies).
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from typing import Any, Literal

import numpy as np
import pandas as pd
from pydantic import BaseModel, ConfigDict, Field

from stonks.core.protocols import Strategy, SurvivalReport
from stonks.lab.backtesting import run_backtest
from stonks.logging import get_logger

_log = get_logger("stonks.lab.survival.pool_correlation")

Member = tuple[str, Strategy]


def _returns(report: Any) -> pd.Series:
    curve = pd.Series(list(report.equity_curve), index=list(report.equity_dates), dtype=float)
    return curve.pct_change(fill_method=None).iloc[1:]


def _ir(returns: pd.Series) -> float:
    values = returns.to_numpy(dtype=float)
    sd = float(np.std(values, ddof=1)) if len(values) > 1 else 0.0
    if not sd > 0 or not math.isfinite(sd):
        return 0.0
    return float(np.mean(values)) / sd * math.sqrt(252)


def _same(a: Any, b: Any) -> bool:
    return type(a) is type(b) and getattr(a, "params", None) == getattr(b, "params", None)


def _configured_pool() -> list[Member]:
    """The active strategies of the configured install (none without one)."""
    from pathlib import Path

    from stonks.config import load_settings
    from stonks.registry.store import StrategyRegistry
    from stonks.store.state import SqliteState

    settings = load_settings()
    if not Path(settings.state.path).exists():
        return []
    with SqliteState(settings.state.path) as state:
        return _registry_pool(
            StrategyRegistry(state=state, artifacts_dir=settings.registry.artifacts_dir)
        )


def _registry_pool(registry: Any) -> list[Member]:
    out: list[Member] = []
    for handle in registry.list_active():
        try:
            out.append((handle.id, registry.load(handle.id)))
        except Exception as exc:
            _log.warning("pool_correlation.load_failed", strategy_id=handle.id, error=str(exc))
    return out


class PoolCorrelationTest:
    id = "pool_correlation"

    class Options(BaseModel):
        model_config = ConfigDict(extra="forbid")

        max_correlation: float = Field(0.7, gt=0.0, le=1.0)
        #: A correlated candidate passes when its IR beats the member's by this share.
        ir_margin: float = Field(0.1, ge=0.0)
        window: Literal["val", "full"] = "val"
        #: Fewest paired daily returns a correlation needs.
        min_bars: int = Field(20, ge=3)

    @classmethod
    def build(cls, options: PoolCorrelationTest.Options) -> PoolCorrelationTest:
        return cls(**options.model_dump())

    def __init__(
        self,
        max_correlation: float = 0.7,
        ir_margin: float = 0.1,
        window: Literal["val", "full"] = "val",
        min_bars: int = 20,
        *,
        pool: Sequence[Member] | None = None,
        registry: Any = None,
    ) -> None:
        self.max_correlation = float(max_correlation)
        self.ir_margin = float(ir_margin)
        self.window = window
        self.min_bars = int(min_bars)
        self._pool = None if pool is None else list(pool)
        self._registry = registry

    def _members(self) -> list[Member]:
        if self._pool is not None:
            return self._pool
        if self._registry is not None:
            return _registry_pool(self._registry)
        return _configured_pool()

    def _beats(self, candidate_ir: float, member_ir: float) -> bool:
        return candidate_ir >= member_ir + self.ir_margin * abs(member_ir)

    def run(self, strategy: Strategy, context: Any) -> SurvivalReport:
        window = context.val_window if self.window == "val" else context.full_window
        members = [(sid, m) for sid, m in self._members() if not _same(m, strategy)]
        candidate = _returns(run_backtest(strategy, context, window, benchmark=None))
        candidate_ir = _ir(candidate)
        metrics: dict[str, float] = {
            "candidate_ir": candidate_ir,
            "pool_size": float(len(members)),
            "max_correlation": 0.0,
        }
        if not members:
            return SurvivalReport(
                test_id=self.id,
                passed=True,
                metrics=metrics,
                notes="no active strategies to compare with",
            )
        blockers: list[str] = []
        skipped: list[str] = []
        worst = -math.inf
        for sid, member in members:
            try:
                theirs = _returns(run_backtest(member, context, window, benchmark=None))
            except Exception as exc:
                _log.warning("pool_correlation.backtest_failed", strategy_id=sid, error=str(exc))
                skipped.append(sid)
                continue
            paired = pd.concat([candidate, theirs], axis=1, join="inner").dropna()
            member_ir = _ir(theirs)
            metrics[f"member_ir:{sid}"] = member_ir
            if len(paired) < self.min_bars or paired.std(ddof=1).min() <= 0:
                skipped.append(sid)
                continue
            corr = float(np.corrcoef(paired.iloc[:, 0], paired.iloc[:, 1])[0, 1])
            metrics[f"correlation:{sid}"] = corr
            worst = max(worst, corr)
            if corr > self.max_correlation and not self._beats(candidate_ir, member_ir):
                blockers.append(f"{sid} (corr {corr:.2f}, IR {member_ir:.2f})")
        metrics["max_correlation"] = worst if math.isfinite(worst) else 0.0
        notes = (
            f"candidate IR {candidate_ir:.2f}, max correlation {metrics['max_correlation']:.2f}"
            f" (limit {self.max_correlation:g}, unless IR is {self.ir_margin:.0%} better)"
        )
        if blockers:
            notes += "; too close to " + ", ".join(blockers)
        if skipped:
            notes += "; skipped " + ", ".join(skipped)
        return SurvivalReport(test_id=self.id, passed=not blockers, metrics=metrics, notes=notes)

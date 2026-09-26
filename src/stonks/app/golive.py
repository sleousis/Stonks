"""GoLiveService — the go-live gate (:mod:`stonks.production.golive`) as a
typed view for transports. Same checks and numbers as ``stonks golive
check``; it only reports and never changes a strategy's status."""

from __future__ import annotations

from datetime import date
from typing import Any, Literal

from pydantic import BaseModel

from stonks.app.context import AppContext
from stonks.app.errors import NotFoundError
from stonks.app.serialize import FiniteFloat
from stonks.config import GoLivePolicy
from stonks.production.golive import PaperSource, evaluate_golive

GoLiveCheckName = Literal[
    "status",
    "min_days",
    "max_drawdown",
    "max_drift",
    "min_trades",
    "survival",
    # incubation grade ([golive] incubation = true, BL-25)
    "within_mc_band",
    "quit_rule",
    "promotion_preset",
    "nonzero_costs",
    "hypothesis_recorded",
    "backtest_min_trades",
]


class GoLiveCheckView(BaseModel):
    name: GoLiveCheckName
    passed: bool
    #: The measured value (days, drawdown fraction, drift, trades, passed
    #: survival reports); None when there is nothing to measure or it is
    #: not finite.
    value: FiniteFloat
    #: The ``[golive]`` limit it is compared against (for ``survival``, the
    #: number of stored reports); None for ``status``, or when not finite
    #: (e.g. an unbounded MinTRL).
    limit: FiniteFloat
    detail: str


class PromotionChecklistView(BaseModel):
    """What a reviewer reads before promoting; it doesn't change the
    verdict. ``None`` for anything not recorded."""

    #: Tuning trials of the lab run's strategy class (P2).
    n_trials_class: int | None = None
    #: Deflated Sharpe ratio of the ``deflated_sharpe`` report.
    dsr: FiniteFloat = None
    #: Probability of backtest overfitting of the ``pbo`` report.
    pbo: FiniteFloat = None
    #: Excess CAGR over the benchmark (``benchmark_relative`` report).
    excess_cagr: FiniteFloat = None
    premortem: str | None = None
    hypothesis: str | None = None


class CostComparisonView(BaseModel):
    """Live shortfall of the strategy's real orders against the cost
    model's estimate (BL-32, P22), in bps."""

    orders: int = 0
    live_is_bps: FiniteFloat = None
    modelled_bps: FiniteFloat = None
    #: Live minus modelled, over the orders that recorded an estimate.
    model_gap_bps: FiniteFloat = None


class GoLiveReport(BaseModel):
    strategy_id: str
    status: str
    #: Where the paper P&L comes from: the strategy's shadow book, the real
    #: portfolio (active strategies share it), or nothing (retired).
    source: PaperSource
    passed: bool
    checks: list[GoLiveCheckView]
    #: Promotion context (trial count, DSR, PBO, benchmark excess, ...).
    checklist: PromotionChecklistView = PromotionChecklistView()
    #: Live costs against the model; null without a real paper period.
    costs: CostComparisonView | None = None
    policy: GoLivePolicy


class GoLiveService:
    def __init__(self, context: AppContext) -> None:
        self._ctx = context

    def check(self, strategy_id: str, since: date | None = None) -> GoLiveReport:
        """Evaluate the strategy's paper period against ``[golive]``.
        ``NotFoundError`` for an unknown id."""
        policy = self._ctx.settings.golive
        with self._ctx.state() as state:
            registry = self._ctx.registry_on(state)
            try:
                report = evaluate_golive(state, registry, strategy_id, policy, since=since)
            except KeyError:
                raise NotFoundError(f"no strategy with id {strategy_id!r}") from None
        return GoLiveReport(
            strategy_id=report.strategy_id,
            status=report.status,
            source=report.source,
            passed=report.passed,
            checks=[
                GoLiveCheckView(
                    name=c.name,  # type: ignore[arg-type]
                    passed=c.passed,
                    value=c.value,
                    limit=c.limit,
                    detail=c.detail,
                )
                for c in report.checks
            ],
            checklist=PromotionChecklistView.model_validate(report.checklist),
            costs=CostComparisonView.model_validate(report.costs) if report.costs else None,
            policy=policy.model_copy(deep=True),
        )


def golive_service(services: Any) -> GoLiveService:
    """A :class:`GoLiveService` over a ``Services`` container's context."""
    return GoLiveService(services.context)

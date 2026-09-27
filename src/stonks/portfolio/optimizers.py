"""Mean-variance with trading costs and a turnover cap (Boyd et al.,
single-period trade optimisation; BL-44).

The problem, in weights ``w`` with current weights ``w0`` and trades
``z = w - w0``::

    maximise  mu'w - risk_aversion * w'Σw
              - trade_aversion * sum_i(a_i |z_i| + c_i |z_i|**1.5)
    subject to lower <= w <= upper,  sum|w| <= max_gross,  sum|z| <= turnover_limit

``a_i`` is the linear cost (half spread plus fees, as a fraction of the
trade) and ``c_i = impact * sigma_i * sqrt(value / V_i)`` the square-root
impact of a name with daily sigma ``sigma_i`` and daily dollar volume
``V_i``, for a book of ``value``.

The solver sits behind the :class:`QpSolver` seam, so no cvxpy type leaves
this module. cvxpy is imported only when a solve is asked for; without it
:class:`OptimizerUnavailable` says how to install it.

The ``mean_variance_costs`` constructor turns combined scores into expected
returns with Grinold's rule, ``mu_i = ic * sigma_i * score_i``, so it wants
z-scored signals (the default ``signal_method``).
"""

from __future__ import annotations

import math
from abc import ABC, abstractmethod
from dataclasses import dataclass, field, replace
from typing import ClassVar

import numpy as np
from pydantic import Field

from stonks.logging import get_logger
from stonks.portfolio._risk_based import (
    RiskBasedSettings,
    covariance_for,
    top_candidates,
)
from stonks.portfolio.base import (
    ConstructionInput,
    PortfolioConstructor,
    TargetBook,
    Ticker,
    register_constructor,
)
from stonks.portfolio.diversification import effective_number_of_bets

_log = get_logger("stonks.portfolio.optimizers")

#: Solver weights smaller than this are noise and become 0.
WEIGHT_EPSILON = 1e-7


class OptimizerUnavailable(RuntimeError):
    """The optimisation library behind a solver isn't installed."""


@dataclass(frozen=True, kw_only=True)
class MeanVarianceProblem:
    """One single-period problem. Arrays are aligned by name; ``None``
    costs mean no such cost and ``turnover_limit=None`` means no cap."""

    mu: np.ndarray
    cov: np.ndarray
    current: np.ndarray
    lower: np.ndarray
    upper: np.ndarray
    max_gross: float = 1.0
    risk_aversion: float = 1.0
    trade_aversion: float = 0.0
    linear_cost: np.ndarray | None = None
    impact_cost: np.ndarray | None = None
    turnover_limit: float | None = None


@dataclass(frozen=True)
class SolverResult:
    weights: np.ndarray
    status: str
    ok: bool = field(default=False)


class QpSolver(ABC):
    """Solves a :class:`MeanVarianceProblem`. :meth:`check` raises
    :class:`OptimizerUnavailable` when the backing library is missing."""

    name: ClassVar[str] = ""

    @abstractmethod
    def check(self) -> None: ...

    @abstractmethod
    def solve(self, problem: MeanVarianceProblem) -> SolverResult: ...


class CvxpySolver(QpSolver):
    """cvxpy with the Clarabel conic solver when present (the default
    install ships it), otherwise cvxpy's own choice."""

    name = "cvxpy"
    _OK = frozenset({"optimal", "optimal_inaccurate"})

    def check(self) -> None:
        self._cvxpy()

    @staticmethod
    def _cvxpy():
        try:
            import cvxpy
        except ImportError as exc:
            raise OptimizerUnavailable(
                "the mean_variance_costs constructor needs cvxpy; install it with "
                "`uv add cvxpy` or pick another [production.construction] method"
            ) from exc
        return cvxpy

    def solve(self, problem: MeanVarianceProblem) -> SolverResult:
        cp = self._cvxpy()
        p = problem
        n = p.mu.size
        w = cp.Variable(n)
        z = w - p.current
        objective = p.mu @ w - p.risk_aversion * cp.quad_form(w, cp.psd_wrap(p.cov))
        if p.trade_aversion > 0:
            costs = 0
            if p.linear_cost is not None and np.any(p.linear_cost > 0):
                costs = costs + p.linear_cost @ cp.abs(z)
            if p.impact_cost is not None and np.any(p.impact_cost > 0):
                costs = costs + p.impact_cost @ cp.power(cp.abs(z), 1.5)
            objective = objective - p.trade_aversion * costs
        constraints = [w >= p.lower, w <= p.upper, cp.norm1(w) <= p.max_gross]
        if p.turnover_limit is not None:
            constraints.append(cp.norm1(z) <= p.turnover_limit)
        prob = cp.Problem(cp.Maximize(objective), constraints)
        solver = "CLARABEL" if "CLARABEL" in cp.installed_solvers() else None
        try:
            prob.solve(solver=solver)
        except cp.error.SolverError as exc:
            return SolverResult(weights=p.current.copy(), status=f"solver_error: {exc}")
        status = str(prob.status)
        if status not in self._OK or w.value is None:
            return SolverResult(weights=p.current.copy(), status=status)
        weights = np.asarray(w.value, dtype=float)
        weights[np.abs(weights) < WEIGHT_EPSILON] = 0.0
        weights = np.clip(weights, p.lower, p.upper)
        return SolverResult(weights=weights, status=status, ok=True)


_SOLVERS: dict[str, type[QpSolver]] = {"cvxpy": CvxpySolver}


def solver_names() -> list[str]:
    return sorted(_SOLVERS)


def get_solver(name: str) -> QpSolver:
    cls = _SOLVERS.get(name)
    if cls is None:
        raise ValueError(f"unknown solver {name!r}; choose one of {solver_names()}")
    return cls()


# --- constructor ------------------------------------------------------------------


class MeanVarianceSettings(RiskBasedSettings):
    ic: float = Field(default=0.05, gt=0.0, le=1.0)
    risk_aversion: float = Field(default=1.0, gt=0.0)
    trade_aversion: float = Field(default=1.0, ge=0.0)
    spread_cost: float = Field(default=0.0005, ge=0.0)
    impact: float = Field(default=1.0, ge=0.0)
    turnover_limit: float | None = Field(default=None, ge=0.0, le=2.0)
    solver: str = "cvxpy"


@register_constructor("mean_variance_costs")
class MeanVarianceCosts(PortfolioConstructor):
    """Mean-variance over the ``top_n`` best positive scores plus every
    tradable held name (a held name without a signal has ``mu = 0``, so the
    optimiser decides how fast to sell it given costs and the turnover cap).

    - ``mu_i = ic * sigma_i * score_i``; covariance as in ``hrp``/``erc``.
    - Weights stay in ``[0, max_weight]`` (``[-max_weight, max_weight]``
      when not long-only) with gross at most ``max_gross``.
    - Impact needs daily share volumes on the input (``volumes``, read
      duck-typed); names without one pay only ``spread_cost``.
    - An infeasible turnover cap (the book already breaks a limit) is
      dropped for that decision and ``meta["turnover_relaxed"]`` is set.
      Any other solver failure holds the current weights.
    - Every held name counts, shorts too. A held name the covariance can't
      model (no history) keeps its current weight rather than being closed
      in one step (``meta["held_unmodelled"]``, BE-34).
    """

    Settings = MeanVarianceSettings

    def __init__(self, settings: MeanVarianceSettings | None = None) -> None:
        super().__init__(settings)
        self.solver = get_solver(self.settings.solver)  # type: ignore[attr-defined]
        self.solver.check()

    def target_weights(self, inp: ConstructionInput) -> TargetBook:
        s: MeanVarianceSettings = self.settings  # type: ignore[assignment]
        chosen, combined, attribution = top_candidates(inp, s.top_n)
        held = [t for t, q in inp.portfolio.positions.items() if abs(q) > 1e-12 and inp.tradable(t)]
        cov = covariance_for(inp, list(chosen) + held, s)
        meta: dict = {
            "covariance": cov.source,
            "observations": cov.observations,
            "turnover_relaxed": False,
        }
        if not cov.tickers:
            meta.update(enb=0.0, solver_status="empty")
            return self.finalize({}, meta=meta)
        problem = self._problem(inp, cov.tickers, cov.matrix, combined, set(chosen))
        result = self.solver.solve(problem)
        if not result.ok and "infeasible" in result.status and s.turnover_limit is not None:
            meta["turnover_relaxed"] = True
            result = self.solver.solve(_without_turnover(problem))
        meta["solver_status"] = result.status
        if not result.ok:
            _log.warning("mean_variance.solver_failed", status=result.status, as_of=str(inp.as_of))
        w = result.weights
        meta["enb"] = effective_number_of_bets(np.abs(w), cov.matrix)
        weights = {
            t: float(x) for t, x in zip(cov.tickers, w, strict=True) if x != 0 and math.isfinite(x)
        }
        unmodelled = sorted(set(held) - set(cov.tickers))
        if unmodelled:
            equity = inp.portfolio.total_value(inp.prices)
            if equity > 0:
                for t in unmodelled:
                    weights[t] = inp.portfolio.positions[t] * inp.prices[t] / equity
            meta["held_unmodelled"] = unmodelled
        return self.finalize(weights, attribution, meta)

    def _problem(
        self,
        inp: ConstructionInput,
        tickers: list[Ticker],
        matrix: np.ndarray,
        combined: dict[Ticker, float],
        chosen: set[Ticker],
    ) -> MeanVarianceProblem:
        s: MeanVarianceSettings = self.settings  # type: ignore[assignment]
        n = len(tickers)
        sigma = np.sqrt(np.diag(matrix))
        scores = np.array([combined.get(t, 0.0) if t in chosen else 0.0 for t in tickers])
        equity = inp.portfolio.total_value(inp.prices)
        current = np.zeros(n)
        if equity > 0:
            current = np.array(
                [inp.portfolio.positions.get(t, 0.0) * inp.prices[t] / equity for t in tickers]
            )
        upper = np.full(n, s.max_weight)
        lower = np.zeros(n) if s.long_only else -upper
        impact = np.zeros(n)
        volumes = getattr(inp, "volumes", None) or {}
        if s.impact > 0 and equity > 0:
            daily = sigma / math.sqrt(s.periods_per_year)
            for i, t in enumerate(tickers):
                v = volumes.get(t)
                if v is not None and math.isfinite(v) and v > 0:
                    impact[i] = s.impact * daily[i] * math.sqrt(equity / (v * inp.prices[t]))
        return MeanVarianceProblem(
            mu=s.ic * sigma * scores,
            cov=matrix,
            current=current,
            lower=lower,
            upper=upper,
            max_gross=s.max_gross,
            risk_aversion=s.risk_aversion,
            trade_aversion=s.trade_aversion,
            linear_cost=np.full(n, s.spread_cost),
            impact_cost=impact,
            turnover_limit=s.turnover_limit,
        )


def _without_turnover(problem: MeanVarianceProblem) -> MeanVarianceProblem:
    return replace(problem, turnover_limit=None)

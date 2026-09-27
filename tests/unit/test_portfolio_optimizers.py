"""Mean-variance with trading costs and a turnover cap (BL-44)."""

from __future__ import annotations

import math
import sys
from datetime import date

import numpy as np
import pytest

from stonks.core.types import Portfolio
from stonks.portfolio.base import ConstructionInput, get_constructor
from stonks.portfolio.optimizers import (
    MeanVarianceProblem,
    OptimizerUnavailable,
    get_solver,
    solver_names,
)

AS_OF = date(2026, 1, 30)


def _problem(**kw) -> MeanVarianceProblem:
    base = {
        "mu": np.array([0.02, 0.01, 0.015]),
        "cov": np.diag([0.04, 0.04, 0.09]),
        "current": np.zeros(3),
        "lower": np.zeros(3),
        "upper": np.ones(3),
        "max_gross": 1.0,
        "risk_aversion": 1.0,
    }
    base.update(kw)
    return MeanVarianceProblem(**base)


def test_cvxpy_is_the_registered_solver() -> None:
    assert "cvxpy" in solver_names()


def test_interior_solution_matches_closed_form() -> None:
    p = _problem()
    result = get_solver("cvxpy").solve(p)
    assert result.ok
    expected = p.mu / (2 * np.diag(p.cov))  # w = mu / (2 gamma sigma^2)
    np.testing.assert_allclose(result.weights, expected, atol=1e-5)


def test_constraints_bind() -> None:
    p = _problem(mu=np.array([1.0, 1.0, 1.0]), upper=np.full(3, 0.4), max_gross=0.9)
    w = get_solver("cvxpy").solve(p).weights
    assert w.sum() <= 0.9 + 1e-6
    assert np.all(w <= 0.4 + 1e-6) and np.all(w >= -1e-8)


def test_turnover_cap_binds() -> None:
    current = np.array([0.0, 0.0, 0.5])
    p = _problem(mu=np.array([1.0, 1.0, -1.0]), current=current, turnover_limit=0.3)
    w = get_solver("cvxpy").solve(p).weights
    assert np.abs(w - current).sum() <= 0.3 + 1e-6


def test_trading_costs_shrink_the_trade() -> None:
    free = get_solver("cvxpy").solve(_problem()).weights
    costly = (
        get_solver("cvxpy")
        .solve(_problem(trade_aversion=1.0, linear_cost=np.full(3, 0.005)))
        .weights
    )
    assert np.abs(costly).sum() < np.abs(free).sum()
    # a linear cost c moves the closed form to (mu - c) / (2 sigma^2)
    np.testing.assert_allclose(
        costly, (_problem().mu - 0.005) / (2 * np.diag(_problem().cov)), atol=1e-5
    )


def test_impact_cost_is_convex_and_shrinks_the_trade() -> None:
    free = get_solver("cvxpy").solve(_problem()).weights
    impact = (
        get_solver("cvxpy")
        .solve(_problem(trade_aversion=1.0, impact_cost=np.full(3, 0.05)))
        .weights
    )
    assert np.all(impact < free)


def test_infeasible_turnover_is_reported() -> None:
    # held 0.9 in one name but the cap is 0.4 and turnover only 0.1
    p = _problem(current=np.array([0.9, 0.0, 0.0]), upper=np.full(3, 0.4), turnover_limit=0.1)
    result = get_solver("cvxpy").solve(p)
    assert not result.ok
    assert "infeasible" in result.status


def test_missing_cvxpy_gives_a_clear_error(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setitem(sys.modules, "cvxpy", None)
    with pytest.raises(OptimizerUnavailable, match="cvxpy"):
        get_solver("cvxpy").solve(_problem())
    with pytest.raises(OptimizerUnavailable, match="cvxpy"):
        get_constructor("mean_variance_costs")


def test_unknown_solver() -> None:
    with pytest.raises(ValueError, match="cvxpy"):
        get_solver("nope")


# --- constructor ----------------------------------------------------------------


def _inp(scores, vols, *, positions=None, cash=1000.0, cls=ConstructionInput, **kw):
    tickers = set(scores) | set(positions or {})
    return cls(
        signals={"s": scores},
        portfolio=Portfolio(cash=cash, positions=positions or {}),
        prices=dict.fromkeys(tickers, 10.0),
        vols_annual=vols,
        as_of=AS_OF,
        **kw,
    )


def test_constructor_uses_grinold_alpha() -> None:
    # alpha = ic * sigma * z, so w = ic * z / (2 gamma sigma)
    vols = {"A": 0.2, "B": 0.4}
    c = get_constructor("mean_variance_costs", ic=0.1, risk_aversion=1.0, trade_aversion=0.0)
    book = c.target_weights(_inp({"A": 1.0, "B": 2.0}, vols))
    assert book.weights == pytest.approx({"A": 0.25, "B": 0.25}, abs=1e-4)
    assert book.meta["solver_status"] == "optimal"
    # variance shares 0.2 and 0.8
    assert book.meta["enb"] == pytest.approx(
        math.exp(-(0.2 * math.log(0.2) + 0.8 * math.log(0.8))), rel=1e-3
    )


def test_constructor_respects_caps_and_turnover() -> None:
    vols = {"A": 0.1, "B": 0.1, "C": 0.1}
    c = get_constructor(
        "mean_variance_costs",
        ic=1.0,
        risk_aversion=0.1,
        max_weight=0.3,
        turnover_limit=0.5,
        trade_aversion=0.0,
    )
    book = c.target_weights(_inp({"A": 3.0, "B": 2.0, "C": 1.0}, vols))
    assert all(0 < w <= 0.3 + 1e-6 for w in book.weights.values())
    assert book.gross <= 0.5 + 1e-6  # from all cash, turnover 0.5


def test_held_name_without_signal_is_sold_gradually_under_costs() -> None:
    vols = {"A": 0.2, "OLD": 0.2}
    positions = {"OLD": 50.0}  # 500 of 1000 equity
    c = get_constructor(
        "mean_variance_costs", ic=0.05, trade_aversion=1.0, spread_cost=0.0, turnover_limit=0.2
    )
    book = c.target_weights(_inp({"A": 1.0}, vols, positions=positions, cash=500.0))
    assert book.weights.get("OLD", 0.0) == pytest.approx(0.3, abs=1e-4)


def test_be34_a_held_short_respects_the_turnover_cap() -> None:
    vols = {"A": 0.2, "SHORT": 0.2}
    positions = {"SHORT": -30.0}  # -300 of 1300 - 300 = 1000 equity: weight -0.3
    c = get_constructor(
        "mean_variance_costs",
        long_only=False,
        ic=0.05,
        trade_aversion=1.0,
        spread_cost=0.0,
        turnover_limit=0.2,
    )
    book = c.target_weights(_inp({"A": 1.0}, vols, positions=positions, cash=1300.0))
    # covered gradually, not in one step
    assert book.weights.get("SHORT", 0.0) < -0.05


def test_be34_a_held_name_without_history_is_held_not_dumped() -> None:
    vols = {"A": 0.2}  # NEW has no volatility: it can't be modelled
    positions = {"NEW": 20.0}  # 200 of 1000
    c = get_constructor("mean_variance_costs", ic=0.05, trade_aversion=1.0, turnover_limit=0.2)
    book = c.target_weights(_inp({"A": 1.0}, vols, positions=positions, cash=800.0))
    assert book.weights.get("NEW", 0.0) == pytest.approx(0.2, abs=1e-6)


def test_infeasible_turnover_is_relaxed_not_crashed() -> None:
    vols = {"A": 0.2, "OLD": 0.2}
    c = get_constructor(
        "mean_variance_costs", max_weight=0.2, turnover_limit=0.05, trade_aversion=0.0
    )
    book = c.target_weights(_inp({"A": 1.0}, vols, positions={"OLD": 90.0}, cash=100.0))
    assert book.meta["turnover_relaxed"] is True
    assert all(w <= 0.2 + 1e-6 for w in book.weights.values())


def test_volumes_add_impact_when_present() -> None:
    vols = {"A": 0.2}
    c = get_constructor("mean_variance_costs", ic=0.2, trade_aversion=1.0, spread_cost=0.0)
    plain = c.target_weights(_inp({"A": 1.0}, vols, cash=1e6)).weights["A"]
    thin = c.target_weights(_inp({"A": 1.0}, vols, cash=1e6, volumes={"A": 100.0})).weights["A"]
    assert thin < plain


def test_deterministic() -> None:
    vols = {"A": 0.2, "B": 0.3, "C": 0.25}
    c = get_constructor("mean_variance_costs")
    one = c.target_weights(_inp({"A": 1.0, "B": 2.0, "C": 0.5}, vols)).weights
    two = c.target_weights(_inp({"C": 0.5, "B": 2.0, "A": 1.0}, vols)).weights
    assert one == two


# --- held shorts and names without data (BE-34) ---------------------------------------


def test_a_held_short_respects_the_turnover_cap() -> None:
    vols = {"A": 0.2, "SHORT": 0.2}
    positions = {"SHORT": -30.0}  # -300 of 1300 equity
    c = get_constructor(
        "mean_variance_costs", long_only=False, ic=0.05, trade_aversion=1.0,
        spread_cost=0.0, turnover_limit=0.1,
    )  # fmt: skip
    book = c.target_weights(_inp({"A": 1.0}, vols, positions=positions, cash=1600.0))
    assert "SHORT" in book.weights
    assert book.weights["SHORT"] < 0  # covered gradually, not in one step


def test_a_held_name_without_history_or_vol_is_not_dumped() -> None:
    vols = {"A": 0.2}  # OLD has neither history nor a vol
    c = get_constructor(
        "mean_variance_costs", ic=0.05, trade_aversion=1.0, spread_cost=0.0, turnover_limit=0.2
    )
    book = c.target_weights(_inp({"A": 1.0}, vols, positions={"OLD": 50.0}, cash=500.0))
    assert book.weights.get("OLD", 0.0) > 0

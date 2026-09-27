"""Go-live gate for a strategy that shorts (roadmap 16.4): it must have been
validated with realistic borrow costs."""

from __future__ import annotations

from stonks.config import GoLivePolicy
from stonks.core.protocols import SurvivalReport
from stonks.production.golive import (
    MIN_BORROW_FEE_ANNUAL,
    PaperPeriod,
    gate_checks,
    load_paper_period,
)
from stonks.strategies.examples.tsmom import TimeSeriesMomentum
from tests.paper_seed import make_env

POLICY = GoLivePolicy(incubation=False)


def _period(params: dict, reports: list[SurvivalReport]) -> PaperPeriod:
    return PaperPeriod(
        strategy_id="s",
        status="shadow",
        source="shadow",
        rows=[],
        trades=0,
        reports=reports,
        params=params,
    )


def _cost_stress(**metrics: float) -> SurvivalReport:
    return SurvivalReport(test_id="cost_stress", passed=True, metrics=metrics)


def _check(period: PaperPeriod):
    return next((c for c in gate_checks(period, POLICY) if c.name == "short_borrow_costs"), None)


def test_long_only_strategy_has_no_borrow_check():
    assert _check(_period({}, [])) is None
    assert _check(_period({"short_mode": "flat"}, [])) is None


def test_short_strategy_passes_with_realistic_borrow_costs():
    check = _check(
        _period(
            {"short_mode": "short"},
            [_cost_stress(borrow_fee_rate=0.005, sharpe_borrow_stress=0.4)],
        )
    )
    assert check is not None and check.passed
    assert check.value == 0.005 and check.limit == MIN_BORROW_FEE_ANNUAL


def test_short_strategy_without_a_borrow_stress_fails():
    check = _check(_period({"short_mode": "short"}, [_cost_stress(sharpe_1x=1.0)]))
    assert check is not None and not check.passed
    assert "borrow" in check.detail


def test_short_strategy_with_a_token_borrow_fee_fails():
    check = _check(
        _period(
            {"short_mode": "short"},
            [_cost_stress(borrow_fee_rate=0.0001, sharpe_borrow_stress=0.4)],
        )
    )
    assert check is not None and not check.passed


def test_short_strategy_that_dies_at_3x_borrow_fails():
    check = _check(
        _period(
            {"short_mode": "short"},
            [_cost_stress(borrow_fee_rate=0.005, sharpe_borrow_stress=-0.1)],
        )
    )
    assert check is not None and not check.passed


def test_short_strategy_without_a_cost_stress_fails():
    check = _check(_period({"short_mode": "short"}, []))
    assert check is not None and not check.passed


def test_load_paper_period_reads_the_artifact_params(tmp_path):
    state, registry = make_env(tmp_path)
    try:
        sid = registry.register(TimeSeriesMomentum({"short_mode": "short"}), reports=[])
        period = load_paper_period(state, registry, sid)
        assert period.params["short_mode"] == "short"
    finally:
        state.close()

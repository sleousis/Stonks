"""Go-live gate (roadmap 4.3): a strategy's paper period against GoLivePolicy."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from stonks.config import GoLivePolicy, Settings
from stonks.core.protocols import SurvivalReport
from stonks.production.golive import evaluate_golive, load_paper_period
from tests.paper_seed import (
    days,
    make_env,
    oos,
    register,
    seed_fills,
    seed_portfolio,
    seed_shadow,
)

#: The legacy gate: these tests cover the six original checks.
POLICY = GoLivePolicy(
    min_days=20,
    max_drawdown=0.15,
    max_drift=0.05,
    min_trades=5,
    require_all_survival_passed=True,
    incubation=False,
)


@pytest.fixture
def env(tmp_path):
    state, registry = make_env(tmp_path)
    yield state, registry
    state.close()


def _on_track(n: int, cagr: float, start_value: float = 10_000.0):
    """Values compounding exactly at ``cagr`` per 365.25 calendar days."""
    ds = days(n)
    return [(d, start_value * (1 + cagr) ** ((d - ds[0]).days / 365.25)) for d in ds]


def _check(report, name):
    return next(c for c in report.checks if c.name == name)


def test_golive_policy_defaults_and_bounds():
    assert Settings().golive == GoLivePolicy()
    p = GoLivePolicy()
    assert (p.min_days, p.min_trades) == (63, 20)
    assert p.incubation is True
    assert p.use_min_trl is True
    assert (p.min_trl_cap_days, p.min_trl_alpha, p.periods_per_year) == (252, 0.05, 252.0)
    assert p.quit_drawdown_multiple == 1.5
    assert p.promotion_preset == "promotion"
    assert (p.min_backtest_trades, p.min_hypothesis_chars) == (30, 20)
    with pytest.raises(ValidationError):
        GoLivePolicy(quit_drawdown_multiple=0.5)
    with pytest.raises(ValidationError):
        GoLivePolicy(min_trl_alpha=1.0)
    with pytest.raises(ValidationError):
        GoLivePolicy(min_days=0)
    with pytest.raises(ValidationError):
        GoLivePolicy(min_trades=0)
    with pytest.raises(ValidationError):
        GoLivePolicy(max_drawdown=0.0)
    with pytest.raises(ValidationError):
        GoLivePolicy(unknown=1)


def test_shadow_strategy_on_track_passes(env):
    state, registry = env
    sid = register(registry, "shadow", [oos(0.5)])
    seed_shadow(state, sid, _on_track(30, 0.5), fills=6)

    report = evaluate_golive(state, registry, sid, POLICY)

    assert report.passed, [c for c in report.checks if not c.passed]
    assert report.source == "shadow"
    assert _check(report, "min_days").value == 30
    assert _check(report, "min_trades").value == 6
    assert _check(report, "max_drift").value == pytest.approx(0.0, abs=1e-9)
    assert _check(report, "max_drawdown").value == pytest.approx(0.0)
    assert _check(report, "survival").passed


def test_no_paper_data_fails_every_data_check(env):
    state, registry = env
    sid = register(registry, "shadow", [oos(0.5)])

    report = evaluate_golive(state, registry, sid, POLICY)

    assert not report.passed
    for name in ("min_days", "max_drawdown", "max_drift", "min_trades"):
        assert not _check(report, name).passed, name


def test_drawdown_beyond_limit_fails(env):
    state, registry = env
    sid = register(registry, "shadow", [oos(0.0)])
    ds = days(25)
    values = [10_000.0] * 10 + [8_000.0] * 5 + [10_000.0] * 10
    seed_shadow(state, sid, list(zip(ds, values, strict=True)), fills=6)

    report = evaluate_golive(state, registry, sid, POLICY)

    dd = _check(report, "max_drawdown")
    assert not dd.passed
    assert dd.value == pytest.approx(0.20)
    assert dd.limit == 0.15
    assert not report.passed


def test_live_vs_backtest_drift_beyond_limit_fails(env):
    state, registry = env
    sid = register(registry, "shadow", [oos(1.0)])  # expects +100%/yr
    seed_shadow(state, sid, _on_track(60, 0.0), fills=6)  # flat live

    report = evaluate_golive(state, registry, sid, POLICY)

    drift = _check(report, "max_drift")
    expected = 2.0 ** (59 / 365.25) - 1
    assert drift.value == pytest.approx(-expected)
    assert not drift.passed


def test_missing_backtest_expectation_fails_drift(env):
    state, registry = env
    sid = register(registry, "shadow", [SurvivalReport(test_id="drift", passed=True, metrics={})])
    seed_shadow(state, sid, _on_track(30, 0.0), fills=6)

    drift = _check(evaluate_golive(state, registry, sid, POLICY), "max_drift")

    assert not drift.passed
    assert drift.value is None
    assert "backtest" in drift.detail


def test_non_finite_backtest_cagr_fails_drift_without_crashing(env):
    state, registry = env
    sid = register(registry, "shadow", [oos(float("inf"))])
    seed_shadow(state, sid, _on_track(30, 0.0), fills=6)

    assert not _check(evaluate_golive(state, registry, sid, POLICY), "max_drift").passed


def test_zero_starting_value_does_not_divide_by_zero(env):
    state, registry = env
    sid = register(registry, "shadow", [oos(0.0)])
    seed_shadow(state, sid, [(d, 0.0) for d in days(25)], fills=6)

    report = evaluate_golive(state, registry, sid, POLICY)

    assert not _check(report, "max_drift").passed
    assert not report.passed


def test_too_few_days_and_trades_fail(env):
    state, registry = env
    sid = register(registry, "shadow", [oos(0.0)])
    seed_shadow(state, sid, _on_track(5, 0.0), fills=2)

    report = evaluate_golive(state, registry, sid, POLICY)

    assert not _check(report, "min_days").passed
    assert not _check(report, "min_trades").passed
    assert _check(report, "min_trades").value == 2


def test_failed_survival_report_fails_unless_not_required(env):
    state, registry = env
    sid = register(
        registry,
        "shadow",
        [oos(0.0), SurvivalReport(test_id="perturbation", passed=False, metrics={})],
    )
    seed_shadow(state, sid, _on_track(30, 0.0), fills=6)

    strict = evaluate_golive(state, registry, sid, POLICY)
    assert not _check(strict, "survival").passed
    assert not strict.passed

    lenient = evaluate_golive(
        state, registry, sid, POLICY.model_copy(update={"require_all_survival_passed": False})
    )
    assert lenient.passed


def test_no_survival_reports_fails_when_required(env):
    state, registry = env
    sid = register(registry, "shadow", [])
    seed_shadow(state, sid, _on_track(30, 0.0), fills=6)

    assert not _check(evaluate_golive(state, registry, sid, POLICY), "survival").passed


def test_active_strategy_uses_real_portfolio_and_its_own_fills(env):
    state, registry = env
    sid = register(registry, "active", [oos(0.5)])
    other = register(registry, "active", [oos(0.5)], params={"ticker": "X.US", "allocation": 1})
    seed_portfolio(state, _on_track(30, 0.5))
    seed_fills(state, sid, 5)
    seed_fills(state, other, 7, ticker="X.US")

    report = evaluate_golive(state, registry, sid, POLICY)

    assert report.source == "portfolio"
    assert _check(report, "min_days").value == 30
    assert _check(report, "min_trades").value == 5
    assert report.passed


def test_since_trims_and_rebases_the_paper_period(env):
    state, registry = env
    sid = register(registry, "shadow", [oos(0.0)])
    ds = days(30)
    values = [20_000.0] * 5 + [10_000.0] * 25  # the crash is before `since`
    seed_shadow(state, sid, list(zip(ds, values, strict=True)), fills=6)

    period = load_paper_period(state, registry, sid, since=ds[5])

    assert period.days == 25
    assert period.max_drawdown == pytest.approx(0.0)
    assert period.period_return == pytest.approx(0.0)


def test_retired_strategy_fails(env):
    state, registry = env
    sid = register(registry, "retired", [oos(0.0)])

    report = evaluate_golive(state, registry, sid, POLICY)

    assert not report.passed
    assert not _check(report, "status").passed


def test_unknown_strategy_raises_key_error(env):
    state, registry = env
    with pytest.raises(KeyError):
        evaluate_golive(state, registry, "nope", POLICY)


def test_gate_never_changes_status(env):
    state, registry = env
    sid = register(registry, "shadow", [oos(0.5)])
    seed_shadow(state, sid, _on_track(30, 0.5), fills=6)

    assert evaluate_golive(state, registry, sid, POLICY).passed
    assert registry.list_all(status="shadow")[0].id == sid

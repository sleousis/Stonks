"""Incubation-grade go-live (BL-25): MinTRL paper period, Monte Carlo band,
quit rule and promotion checklist on top of the legacy gate."""

from __future__ import annotations

import dataclasses
import json
import math

import pytest
from pydantic import ValidationError

from stonks.config import GoLivePolicy
from stonks.core.protocols import SurvivalReport
from stonks.production.golive import (
    IncubationPolicy,
    evaluate_golive,
    gate_checks,
    load_paper_period,
)
from stonks.stats.sharpe import min_trl
from stonks.strategies.examples.buy_and_hold import BuyAndHold
from tests.fixtures.w2_6 import (
    HYPOTHESIS,
    ZERO_COSTS,
    mc_report,
    oos_report,
    promotion_reports,
    set_meta,
)
from tests.paper_seed import days, make_env, register, seed_shadow

LEGACY_NAMES = ["status", "min_days", "max_drawdown", "max_drift", "min_trades", "survival"]
INCUBATION_NAMES = [
    *LEGACY_NAMES,
    "within_mc_band",
    "quit_rule",
    "promotion_preset",
    "nonzero_costs",
    "hypothesis_recorded",
    "backtest_min_trades",
]
POLICY = IncubationPolicy()


@pytest.fixture
def env(tmp_path):
    state, registry = make_env(tmp_path)
    yield state, registry
    state.close()


def _flat(n: int, value: float = 10_000.0):
    return [(d, value) for d in days(n)]


def _with_dip(n: int, dd: float):
    """Flat, a dip of ``dd`` in the middle, then back to the start value."""
    pts = _flat(n)
    mid = n // 2
    pts[mid] = (pts[mid][0], 10_000.0 * (1 - dd))
    return pts


def _check(report, name):
    return next(c for c in report.checks if c.name == name)


def _seed(env, reports=None, points=None, fills=25, meta=True, **meta_kw):
    state, registry = env
    sid = register(registry, "shadow", reports if reports is not None else promotion_reports())
    if meta:
        set_meta(registry, sid, **meta_kw)
    seed_shadow(state, sid, points if points is not None else _flat(80), fills=fills)
    return sid


def _evaluate(env, sid, policy=POLICY):
    state, registry = env
    return evaluate_golive(state, registry, sid, policy)


# ---- policy ------------------------------------------------------------------


def test_incubation_policy_defaults():
    p = IncubationPolicy()
    assert isinstance(p, GoLivePolicy)
    assert p.incubation is True
    assert (p.min_days, p.min_trades) == (63, 20)
    assert p.use_min_trl is True
    assert p.min_trl_cap_days == 252
    assert p.min_trl_alpha == 0.05
    assert p.quit_drawdown_multiple == 1.5
    assert p.promotion_preset == "promotion"
    assert p.min_backtest_trades == 30
    assert p.min_hypothesis_chars == 20
    with pytest.raises(ValidationError):
        IncubationPolicy(unknown=1)
    with pytest.raises(ValidationError):
        IncubationPolicy(quit_drawdown_multiple=0.5)


def test_legacy_policy_keeps_the_legacy_report_shape(env):
    sid = _seed(env)
    report = _evaluate(env, sid, GoLivePolicy())
    assert [c.name for c in report.checks] == LEGACY_NAMES


def test_policy_with_incubation_field_turns_on_new_checks(env):
    """What the integration step does: add the fields to ``GoLivePolicy``."""

    class Extended(GoLivePolicy):
        incubation: bool = True

    sid = _seed(env)
    report = _evaluate(env, sid, Extended(min_days=63, min_trades=20))
    assert [c.name for c in report.checks] == INCUBATION_NAMES
    assert report.passed, report.failures


# ---- the whole gate ------------------------------------------------------------


def test_on_track_incubation_passes_every_check(env):
    sid = _seed(env)
    report = _evaluate(env, sid)
    assert [c.name for c in report.checks] == INCUBATION_NAMES
    assert report.passed, report.failures
    for c in report.checks[1:]:
        assert c.value is not None and c.limit is not None, c.name


def test_nothing_recorded_fails_every_incubation_check(env):
    sid = _seed(env, reports=[], points=[], fills=0, meta=False)
    report = _evaluate(env, sid)
    failed = {c.name for c in report.failures}
    assert set(INCUBATION_NAMES) - {"status"} <= failed


# ---- MinTRL paper period --------------------------------------------------------


def test_low_sharpe_raises_day_requirement_to_the_cap(env):
    sid = _seed(env, reports=promotion_reports(oos=oos_report(sharpe=0.5)))
    check = _check(_evaluate(env, sid), "min_days")
    assert check.limit == 252
    assert not check.passed
    assert "MinTRL" in check.detail


def test_mid_sharpe_requirement_is_the_min_trl(env):
    sid = _seed(env, reports=promotion_reports(oos=oos_report(sharpe=2.5)))
    check = _check(_evaluate(env, sid), "min_days")
    expected = math.ceil(min_trl(2.5 / math.sqrt(252), 0.0, 0.0, 3.0))
    assert 63 < expected < 252
    assert check.limit == expected
    assert not check.passed  # 80 paper days are short of ~109


def test_high_sharpe_keeps_the_floor(env):
    sid = _seed(env, reports=promotion_reports(oos=oos_report(sharpe=6.0)))
    check = _check(_evaluate(env, sid), "min_days")
    assert check.limit == 63
    assert check.passed


def test_negative_sharpe_needs_the_cap(env):
    sid = _seed(env, reports=promotion_reports(oos=oos_report(sharpe=-1.0)))
    assert _check(_evaluate(env, sid), "min_days").limit == 252


def test_stored_min_trl_bars_wins(env):
    sid = _seed(env, reports=promotion_reports(oos=oos_report(sharpe=6.0, min_trl_bars=100.4)))
    check = _check(_evaluate(env, sid), "min_days")
    assert check.limit == 101
    assert not check.passed


def test_measured_skew_and_kurtosis_are_used(env):
    oos = oos_report(sharpe=2.5, skew=-1.0, kurtosis=8.0)
    sid = _seed(env, reports=promotion_reports(oos=oos))
    check = _check(_evaluate(env, sid), "min_days")
    expected = math.ceil(min_trl(2.5 / math.sqrt(252), 0.0, -1.0, 8.0))
    assert check.limit == min(expected, 252)


def test_missing_backtest_sharpe_fails_min_days(env):
    bare = SurvivalReport(test_id="oos", passed=True, metrics={"cagr_oos": 0.0})
    sid = _seed(env, reports=promotion_reports(oos=bare), points=_flat(300))
    check = _check(_evaluate(env, sid), "min_days")
    assert not check.passed
    assert "MinTRL unavailable" in check.detail


def test_min_trl_off_uses_min_days(env):
    sid = _seed(env, reports=promotion_reports(oos=oos_report(sharpe=0.5)))
    check = _check(_evaluate(env, sid, IncubationPolicy(use_min_trl=False)), "min_days")
    assert check.limit == 63
    assert check.passed


# ---- Monte Carlo band -------------------------------------------------------------


def test_drawdown_inside_the_band_passes(env):
    sid = _seed(env, points=_with_dip(80, 0.05))
    check = _check(_evaluate(env, sid), "within_mc_band")
    assert check.passed
    assert check.value == pytest.approx(0.05)
    assert check.limit == pytest.approx(0.20)


def test_drawdown_outside_the_band_fails(env):
    reports = promotion_reports(oos=oos_report(max_dd=-0.5), mc=mc_report(0.10))
    sid = _seed(env, reports=reports, points=_with_dip(80, 0.12))
    check = _check(_evaluate(env, sid), "within_mc_band")
    assert not check.passed
    assert check.limit == pytest.approx(0.10)


def test_negative_signed_p95_is_read_as_a_magnitude(env):
    sid = _seed(env, reports=promotion_reports(mc=mc_report(-0.20)), points=_with_dip(80, 0.05))
    check = _check(_evaluate(env, sid), "within_mc_band")
    assert check.passed
    assert check.limit == pytest.approx(0.20)


def test_missing_monte_carlo_fails_the_band(env):
    reports = [r for r in promotion_reports() if r.test_id != "mc_trades"]
    sid = _seed(env, reports=reports)
    check = _check(_evaluate(env, sid), "within_mc_band")
    assert not check.passed
    assert "Monte Carlo band unavailable" in check.detail


def test_no_paper_snapshots_fails_the_band(env):
    sid = _seed(env, points=[])
    check = _check(_evaluate(env, sid), "within_mc_band")
    assert not check.passed
    assert check.value is None


def test_return_below_the_monte_carlo_floor_fails(env):
    mc = mc_report(0.50, p5_return=0.10)  # worst 5% of years still make +10%
    pts = [(d, 10_000.0 * (1 - 0.001 * i)) for i, d in enumerate(days(80))]
    sid = _seed(env, reports=promotion_reports(mc=mc), points=pts)
    check = _check(_evaluate(env, sid), "within_mc_band")
    assert not check.passed
    assert "return" in check.detail


# ---- quit rule --------------------------------------------------------------------


def test_quit_rule_uses_one_and_a_half_backtest_drawdowns(env):
    reports = promotion_reports(oos=oos_report(max_dd=-0.10), mc=mc_report(0.30))
    sid = _seed(env, reports=reports, points=_with_dip(80, 0.16))
    check = _check(_evaluate(env, sid), "quit_rule")
    assert not check.passed
    assert check.limit == pytest.approx(0.15)
    assert check.value == pytest.approx(0.16)


def test_quit_rule_uses_the_tighter_monte_carlo_limit(env):
    reports = promotion_reports(oos=oos_report(max_dd=-0.20), mc=mc_report(0.12))
    sid = _seed(env, reports=reports, points=_with_dip(80, 0.13))
    check = _check(_evaluate(env, sid), "quit_rule")
    assert not check.passed
    assert check.limit == pytest.approx(0.12)


def test_quit_rule_without_backtest_drawdown_fails(env):
    bare = SurvivalReport(test_id="oos", passed=True, metrics={"sharpe_oos": 4.0})
    sid = _seed(env, reports=promotion_reports(oos=bare))
    check = _check(_evaluate(env, sid), "quit_rule")
    assert not check.passed
    assert "backtest max drawdown" in check.detail


# ---- promotion checklist ----------------------------------------------------------


def test_missing_preset_test_fails_the_checklist(env):
    reports = [r for r in promotion_reports() if r.test_id != "walk_forward"]
    sid = _seed(env, reports=reports)
    check = _check(_evaluate(env, sid), "promotion_preset")
    assert not check.passed
    assert "walk_forward" in check.detail
    assert check.value == check.limit - 1


def test_unknown_preset_fails_rather_than_raising(env):
    sid = _seed(env)
    check = _check(
        _evaluate(env, sid, IncubationPolicy(promotion_preset="nope")), "promotion_preset"
    )
    assert not check.passed


def test_zero_costs_fail(env):
    sid = _seed(env, costs=ZERO_COSTS)
    check = _check(_evaluate(env, sid), "nonzero_costs")
    assert not check.passed
    assert check.value == 0


def test_missing_cost_record_fails(env):
    sid = _seed(env, meta=False)
    check = _check(_evaluate(env, sid), "nonzero_costs")
    assert not check.passed
    assert check.value is None


def test_missing_hypothesis_fails(env):
    sid = _seed(env, hypothesis=None)
    check = _check(_evaluate(env, sid), "hypothesis_recorded")
    assert not check.passed
    assert check.value == 0


def test_short_hypothesis_fails(env):
    sid = _seed(env, hypothesis="goes up")
    assert not _check(_evaluate(env, sid), "hypothesis_recorded").passed


def test_strategy_class_hypothesis_counts(env, monkeypatch):
    monkeypatch.setattr(BuyAndHold, "hypothesis", HYPOTHESIS, raising=False)
    sid = _seed(env, hypothesis=None)
    assert _check(_evaluate(env, sid), "hypothesis_recorded").passed


def test_too_few_backtest_trades_fail(env):
    sid = _seed(env, reports=promotion_reports(oos=oos_report(n_trades=12)))
    check = _check(_evaluate(env, sid), "backtest_min_trades")
    assert not check.passed
    assert (check.value, check.limit) == (12, 30)


def test_missing_backtest_trade_count_fails(env):
    sid = _seed(env, reports=promotion_reports(oos=oos_report(n_trades=None)))
    check = _check(_evaluate(env, sid), "backtest_min_trades")
    assert not check.passed
    assert check.value is None


def test_monte_carlo_trade_count_is_a_fallback(env):
    reports = promotion_reports(oos=oos_report(n_trades=None), mc=mc_report(0.2, n_trades=40))
    sid = _seed(env, reports=reports)
    assert _check(_evaluate(env, sid), "backtest_min_trades").value == 40


# ---- checklist fields and report shape ---------------------------------------------


def test_report_carries_the_checklist_fields(env):
    reports = [
        *promotion_reports(),
        SurvivalReport(test_id="deflated_sharpe", passed=True, metrics={"dsr": 0.97}),
        SurvivalReport(test_id="pbo", passed=True, metrics={"pbo": 0.12}),
        SurvivalReport(test_id="benchmark_relative", passed=True, metrics={"excess_cagr": 0.03}),
    ]
    sid = _seed(env, reports=reports, n_trials_total=42, premortem="Fails if rates spike.")
    checklist = _evaluate(env, sid).checklist
    assert checklist == {
        "n_trials_class": 42,
        "dsr": 0.97,
        "pbo": 0.12,
        "excess_cagr": 0.03,
        "premortem": "Fails if rates spike.",
        "hypothesis": HYPOTHESIS,
    }


def test_checklist_fields_are_none_when_absent(env):
    sid = _seed(env, meta=False)
    checklist = _evaluate(env, sid).checklist
    assert set(checklist) == {
        "n_trials_class",
        "dsr",
        "pbo",
        "excess_cagr",
        "premortem",
        "hypothesis",
    }
    assert all(v is None for v in checklist.values())


def test_gate_checks_signature_still_works(env):
    state, registry = env
    sid = _seed(env)
    period = load_paper_period(state, registry, sid)
    assert [c.name for c in gate_checks(period, POLICY)] == INCUBATION_NAMES


def test_passing_incubation_report_promotes(env):
    state, registry = env
    sid = _seed(env)
    report = _evaluate(env, sid)
    change = registry.set_status(sid, "active", actor="cli", golive_report=report)
    assert change is not None and change.golive_passed is True
    stored = json.loads(
        state.sql("SELECT golive_report_json FROM status_changes ORDER BY id DESC LIMIT 1")[0][
            "golive_report_json"
        ]
    )
    assert stored["checklist"]["hypothesis"] == HYPOTHESIS
    assert [c["name"] for c in stored["checks"]] == INCUBATION_NAMES
    assert dataclasses.asdict(report)["checks"][0]["name"] == "status"


def test_failing_incubation_report_blocks_promotion(env):
    from stonks.registry.store import PromotionRefused

    state, registry = env
    sid = _seed(env, costs=ZERO_COSTS)
    with pytest.raises(PromotionRefused, match="nonzero_costs"):
        registry.set_status(sid, "active", actor="cli", golive_report=_evaluate(env, sid))

"""Go-live check details use plain trader words (console vocabulary, 18.6).

The console shows each check's ``detail`` as is, so it must not carry the
system's names (shadow, promotion, survival, snake_case metric keys) that
``web/scripts/check-copy.mjs`` keeps out of trader copy.
"""

from __future__ import annotations

import re
from datetime import date, timedelta

import pytest

from stonks.config import GoLivePolicy
from stonks.core.protocols import SurvivalReport
from stonks.production.golive import (
    IncubationPolicy,
    PaperPeriod,
    evaluate_golive,
    gate_checks,
)
from stonks.production.pnl import daily_pnl
from tests.fixtures.w2_6 import ZERO_COSTS, mc_report, oos_report, promotion_reports, set_meta
from tests.paper_seed import days, make_env, register, seed_shadow

#: The system words of check-copy.mjs, plus what only code reads:
#: snake_case keys, "(s)" plurals, comparison signs and "P&L".
SYSTEM_WORDS = re.compile(
    r"\bticks?\b|\bingest|\bshadow\b|\bpromot|\bregist|\bretir|\bsurvival\b|\boos\b"
    r"|\b[a-z0-9]+_[a-z0-9_]+\b|\(s\)|>=|<=|P&L",
    re.IGNORECASE,
)


@pytest.fixture
def env(tmp_path):
    state, registry = make_env(tmp_path)
    yield state, registry
    state.close()


def _flat(n: int, value: float = 10_000.0):
    return [(d, value) for d in days(n)]


def _with_dip(n: int, dd: float):
    pts = _flat(n)
    pts[n // 2] = (pts[n // 2][0], 10_000.0 * (1 - dd))
    return pts


def _details(env, status="shadow", reports=(), points=(), fills=0, policy=None, **meta):
    state, registry = env
    sid = register(registry, status, list(reports))
    if meta:
        set_meta(registry, sid, **meta)
    seed_shadow(state, sid, list(points), fills=fills)
    report = evaluate_golive(state, registry, sid, policy or IncubationPolicy())
    return {c.name: c.detail for c in report.checks}


def _assert_plain(details: dict[str, str]) -> None:
    for name, detail in details.items():
        assert detail, name
        found = SYSTEM_WORDS.search(detail)
        assert found is None, f"{name}: {found.group(0)!r} in {detail!r}"


def test_a_passing_gate_reads_in_plain_words(env):
    details = _details(
        env, reports=promotion_reports(), points=_flat(80), fills=25, hypothesis="x" * 40
    )
    _assert_plain(details)
    assert details["status"] == "Paper trading, measured on its paper trading results"
    assert details["min_trades"] == "25 paper trades filled, needs at least 20"
    assert details["survival"].startswith(f"{len(promotion_reports())} of ")


def test_a_gate_with_nothing_recorded_reads_in_plain_words(env):
    details = _details(env)
    _assert_plain(details)
    assert details["survival"] == "No robustness tests on record"
    assert details["max_drawdown"] == "No paper trading results yet"
    assert details["min_days"].startswith("0 days of paper trading")


def test_failing_checks_read_in_plain_words(env):
    reports = [
        r
        for r in promotion_reports(oos=oos_report(max_dd=-0.05), mc=mc_report(0.5))
        if r.test_id != "walk_forward"
    ]
    reports.append(SurvivalReport(test_id="walk_forward_mcpt", passed=False, metrics={}))
    details = _details(
        env,
        reports=reports,
        points=_with_dip(80, 0.2),
        fills=3,
        costs=ZERO_COSTS,
        hypothesis="short",
    )
    _assert_plain(details)
    assert details["promotion_preset"].endswith("on record, missing: Walk-forward")
    assert "failed: Walk-forward permutation" in details["survival"]
    assert details["quit_rule"].startswith("Quit rule tripped, stop paper trading")
    assert details["nonzero_costs"] == "The backtest ran with zero costs"


def test_min_trl_details_read_in_plain_words(env):
    low = _details(env, reports=promotion_reports(oos=oos_report(sharpe=0.5)), points=_flat(10))
    _assert_plain(low)
    assert "minimum track record" in low["min_days"]
    bare = SurvivalReport(test_id="oos", passed=True, metrics={"cagr_oos": 0.0})
    missing = _details(env, reports=[bare])
    _assert_plain(missing)
    assert "cannot work out the minimum track record" in missing["min_days"]


def test_live_and_stopped_strategies_read_in_trader_words(env):
    live = _details(env, status="active", policy=GoLivePolicy(incubation=False))
    stopped = _details(env, status="retired", policy=GoLivePolicy(incubation=False))
    _assert_plain(live)
    _assert_plain(stopped)
    assert live["status"] == "Live, measured on the main portfolio's results"
    assert stopped["status"] == "Stopped, so it has no paper trading record"


def test_short_borrow_details_read_in_plain_words():
    policy = GoLivePolicy(incubation=False)
    rows = daily_pnl([(date(2026, 1, 1) + timedelta(days=i), 10_000.0) for i in range(3)])

    def detail(reports):
        period = PaperPeriod(
            strategy_id="s",
            status="shadow",
            source="shadow",
            rows=rows,
            trades=0,
            reports=reports,
            params={"short_mode": "short"},
        )
        return next(c.detail for c in gate_checks(period, policy) if c.name == "short_borrow_costs")

    stress = "cost_stress"
    for reports in (
        [],
        [SurvivalReport(test_id=stress, passed=True, metrics={"sharpe_1x": 1.0})],
        [
            SurvivalReport(
                test_id=stress,
                passed=True,
                metrics={"borrow_fee_rate": 0.001, "sharpe_borrow_stress": -0.1},
            )
        ],
        [
            SurvivalReport(
                test_id=stress,
                passed=True,
                metrics={"borrow_fee_rate": 0.005, "sharpe_borrow_stress": 0.4},
            )
        ],
    ):
        _assert_plain({"short_borrow_costs": detail(reports)})

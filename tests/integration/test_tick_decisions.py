"""The tick records why each ticker did or did not trade (roadmap 23.7)."""

from __future__ import annotations

from datetime import date

from stonks.production.decisions import load_decisions
from stonks.production.decisions_settings import DecisionSettings
from stonks.production.risk import RiskPolicy
from stonks.production.tick import TickSettings, run_tick

AS_OF = date(2026, 3, 20)


def test_a_real_tick_records_the_risk_rule_that_trimmed_a_buy(tick_env):
    lake, state, registry = tick_env
    settings = TickSettings(
        universe=["UP.US"], initial_cash=10_000.0, risk=RiskPolicy(max_weight_per_ticker=0.3)
    )
    result = run_tick(state, lake, registry, settings, as_of=AS_OF)
    [row] = load_decisions(state, "pf_default", ticker="UP.US")
    assert row.tick_id == result.tick_id
    assert row.as_of == AS_OF
    assert row.decision.step == "risk_rule"
    assert row.decision.outcome == "trimmed"
    assert row.decision.detail["rule"] == "max_weight_per_ticker"


def test_a_rule_that_drops_every_order_is_named(tick_env):
    lake, state, registry = tick_env
    settings = TickSettings(
        universe=["UP.US"], initial_cash=10_000.0, risk=RiskPolicy(max_open_positions=0)
    )
    run_tick(state, lake, registry, settings, as_of=AS_OF)
    [row] = load_decisions(state, "pf_default", ticker="UP.US")
    assert (row.decision.step, row.decision.outcome) == ("risk_rule", "kept_out")
    assert row.decision.detail["rule"] == "max_open_positions"


def test_dry_run_and_disabled_record_nothing(tick_env):
    lake, state, registry = tick_env
    run_tick(state, lake, registry, TickSettings(universe=["UP.US"]), as_of=AS_OF, dry_run=True)
    assert state.count_rows("trade_decisions") == 0
    off = TickSettings(universe=["UP.US"], decisions=DecisionSettings(enabled=False))
    run_tick(state, lake, registry, off, as_of=AS_OF)
    assert state.count_rows("trade_decisions") == 0


def test_a_failing_explanation_loses_only_the_decision_rows(tick_env, monkeypatch):
    """The explanation is a nicety: when it breaks, the book still trades
    and only the ``trade_decisions`` rows are missing."""
    import stonks.production.tick as tick_module

    def broken(*_a, **_k):
        raise RuntimeError("explain broke")

    monkeypatch.setattr(tick_module, "explain_decisions", broken)
    lake, state, registry = tick_env
    result = run_tick(
        state, lake, registry, TickSettings(universe=["UP.US"], initial_cash=10_000.0),
        as_of=AS_OF,
    )  # fmt: skip
    assert result.status == "ok"
    assert state.count_rows("orders") >= 1
    assert state.count_rows("trade_decisions") == 0

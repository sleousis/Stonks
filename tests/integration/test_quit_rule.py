"""The quit rule (BL-29): an active strategy whose attributed drawdown since
promotion goes past its backtest drawdown is flagged, and optionally
demoted to shadow with an audit row."""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta

import pandas as pd
import pytest

from stonks.core.protocols import SurvivalReport
from stonks.production.hooks import TickHookContext, registered_hooks
from stonks.production.quit_rule import (
    QuitRuleSettings,
    apply_quit_rule,
    attributed_drawdown,
    quit_limit,
)
from stonks.registry.store import StrategyRegistry
from stonks.store.state import SqliteState
from stonks.strategies.examples.buy_and_hold import BuyAndHold
from tests.fixtures.governance import seed_status

#: A pinned day (TT-06). The registry's clock logs the promotion on it, so
#: the book starts here whatever the wall clock says.
START = date(2026, 3, 2)


def _clock() -> datetime:
    return datetime(START.year, START.month, START.day, 15, 0, tzinfo=UTC)


def _closes(values: list[float], start: date = START) -> pd.Series:
    idx = pd.to_datetime([start + timedelta(days=i) for i in range(len(values))])
    return pd.Series(values, index=idx, dtype=float)


# ---- pure parts -------------------------------------------------------------------


def test_the_limit_is_the_tighter_of_the_multiple_and_the_monte_carlo_p95():
    reports = {"oos": {"max_drawdown_oos": -0.10}, "mc_trades": {"p95_max_dd": 0.12}}
    assert quit_limit(reports, QuitRuleSettings()) == pytest.approx((0.12, "mc_trades.p95_max_dd"))
    reports["mc_trades"]["p95_max_dd"] = 0.30
    assert quit_limit(reports, QuitRuleSettings()) == pytest.approx((0.15, "oos_multiple"))


def test_a_missing_monte_carlo_report_falls_back_to_the_multiple():
    limit = quit_limit({"oos": {"max_drawdown_oos": -0.2}}, QuitRuleSettings(quit_multiple=2.0))
    assert limit == pytest.approx((0.4, "oos_multiple"))
    assert quit_limit({}, QuitRuleSettings()) is None


def test_attributed_drawdown_uses_the_share_in_force_each_day():
    # 10 shares of X from day 0, half of them this strategy's.
    rows = [("pf", START, "X", 10.0, 0.5)]
    closes = {"X": _closes([100.0, 110.0, 90.0, 95.0])}
    dd = attributed_drawdown(rows, closes, since=START, as_of=START + timedelta(days=3))
    # pnl path: +50, -100 (from peak +50 to -50); exposure mean (550+450+475)/3
    assert dd == pytest.approx(100.0 / ((550 + 450 + 475) / 3))


def test_no_attribution_means_no_drawdown():
    assert attributed_drawdown([], {}, since=START, as_of=START) == 0.0


# ---- with the state ---------------------------------------------------------------


@pytest.fixture
def env(tmp_path):
    state = SqliteState(tmp_path / "state.sqlite")
    state.migrate()
    registry = StrategyRegistry(state=state, artifacts_dir=tmp_path / "artifacts", clock=_clock)
    registry.register(
        BuyAndHold({"ticker": "X", "allocation": 1.0}),
        reports=[
            SurvivalReport(test_id="oos", passed=True, metrics={"max_drawdown_oos": -0.05}),
        ],
        strategy_id="bh",
    )
    seed_status(registry, "bh", "active")
    state.execute(
        "INSERT INTO tick_runs (id, started_at, status) VALUES ('t1', ?, 'ok')", [START.isoformat()]
    )
    state.execute(
        "INSERT INTO position_attribution (tick_id, portfolio_id, as_of, ticker, strategy_id,"
        " quantity, weight_share, source, created_at)"
        " VALUES ('t1', 'pf_default', ?, 'X', 'bh', 10, 1.0, 'decision', ?)",
        [START.isoformat(), START.isoformat()],
    )
    yield state, registry
    state.close()


def _loader(values):
    def load(tickers, as_of, bars):
        return {"X": pd.DataFrame({"close": _closes(values)})} if "X" in tickers else {}

    return load


def test_a_drawdown_past_the_limit_alerts_the_admins(env):
    state, _ = env
    checks = apply_quit_rule(
        state, _loader([100.0, 80.0]), START + timedelta(days=1), QuitRuleSettings()
    )
    [check] = checks
    assert check.strategy_id == "bh" and check.breached and not check.demoted
    [row] = state.sql("SELECT user_id, level, title FROM notification_outbox")
    assert row["level"] == "error" and "bh" in row["title"]
    assert state.sql("SELECT status FROM strategies WHERE id = 'bh'")[0][0] == "active"


def test_auto_demote_moves_the_strategy_to_shadow_with_an_audit_row(env):
    state, registry = env
    [check] = apply_quit_rule(
        state,
        _loader([100.0, 80.0]),
        START + timedelta(days=1),
        QuitRuleSettings(auto_demote=True),
        registry=registry,
    )
    assert check.demoted
    assert state.sql("SELECT status FROM strategies WHERE id = 'bh'")[0][0] == "shadow"
    [row] = state.sql(
        "SELECT actor, to_status, reason FROM status_changes"
        " WHERE strategy_id = 'bh' ORDER BY id DESC LIMIT 1"
    )
    assert (row["actor"], row["to_status"]) == ("system", "shadow")
    assert "quit rule" in row["reason"]


def test_a_small_drawdown_changes_nothing(env):
    state, _ = env
    [check] = apply_quit_rule(
        state, _loader([100.0, 99.0]), START + timedelta(days=1), QuitRuleSettings()
    )
    assert not check.breached
    assert state.sql("SELECT COUNT(*) FROM notification_outbox")[0][0] == 0


def test_the_promotion_day_comes_from_the_registry_clock(env):
    state, _ = env
    [check] = apply_quit_rule(
        state, _loader([100.0, 99.0]), START + timedelta(days=1), QuitRuleSettings()
    )
    assert check.promoted_on == START
    [row] = state.sql("SELECT created_at FROM status_changes WHERE to_status = 'active'")
    assert row["created_at"] == "2026-03-02T15:00:00+00:00"


def test_the_hook_is_registered_and_skips_dry_runs(env):
    state, _ = env
    [hook] = [h for h in registered_hooks("tick") if h.name == "quit_rule"]
    ctx = TickHookContext(
        state=state, lake=None, tick_id="t", as_of=START, dry_run=True, signals=None, portfolios={}
    )
    assert hook.run(ctx) is None


def test_a_breach_queues_deliveries_on_the_configured_channels(env):
    from datetime import UTC, datetime

    from stonks.notify.prefs import PreferenceStore

    state, _ = env
    PreferenceStore(state).set_webhook(
        "usr_owner", "https://hooks.example.test/x", now=datetime.now(UTC)
    )
    apply_quit_rule(state, _loader([100.0, 80.0]), START + timedelta(days=1), QuitRuleSettings())
    channels = [r["channel"] for r in state.sql("SELECT channel FROM notification_deliveries")]
    assert channels == ["webhook"]


# ---- BE-24: a failed demotion ------------------------------------------------------------


class _RefusingRegistry:
    def set_status(self, *args, **kwargs):
        raise RuntimeError("state is locked")


@pytest.mark.parametrize("registry", [None, _RefusingRegistry()])
def test_be24_a_failed_demotion_reports_not_demoted_and_changes_nothing(env, registry):
    state, _ = env
    [check] = apply_quit_rule(
        state,
        _loader([100.0, 80.0]),
        START + timedelta(days=1),
        QuitRuleSettings(auto_demote=True),
        registry=registry,
    )
    assert check.breached and not check.demoted
    assert state.sql("SELECT status FROM strategies WHERE id = 'bh'")[0][0] == "active"
    # the alert still went out
    assert state.sql("SELECT COUNT(*) FROM notification_outbox")[0][0] == 1

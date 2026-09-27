"""The intraday halt helpers (roadmap 21.3.2): the halt check the event
driver runs on every event, and the trips the intraday rules lead to."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from stonks.core.types import Portfolio
from stonks.production.halts import active_halts, clear_halt, trip_halt
from stonks.production.intraday_halts import (
    event_verdict,
    gate_event_orders,
    trip_intraday_loss,
    trip_intraday_runaway,
)
from stonks.production.rules import RiskAdjustment
from stonks.production.rules._intraday import IntradayContext
from stonks.store.state import SqliteState
from tests.fixtures.risk_rules import buy, context, policy, sell

NOW = datetime(2026, 9, 28, 15, 0, tzinfo=UTC)
PF = "pf_default"


@pytest.fixture
def state(tmp_path):
    s = SqliteState(tmp_path / "state.sqlite")
    s.migrate()
    yield s
    s.close()


# ---- the check on every event --------------------------------------------------------


def test_no_halt_lets_every_order_through(state):
    verdict = event_verdict(state, PF, now=NOW)
    assert verdict.mode is None and not verdict.cancel_working
    orders = [buy("A.US", 1, 1), sell("B.US", 1, 2)]
    assert gate_event_orders(orders, verdict, {"B.US": 1.0}) == (orders, [])


def test_a_buys_halt_blocks_opening_orders_and_keeps_closes(state):
    trip_halt(state, "intraday_loss", reason="loss", actor="system", portfolio_id=PF)
    verdict = event_verdict(state, PF, now=NOW)
    assert verdict.mode == "buys"
    assert "intraday_loss" in verdict.reasons[0]
    orders = [buy("A.US", 1, 1), sell("B.US", 1, 2), sell("C.US", 1, 3), buy("D.US", 2, 4)]
    positions = {"B.US": 1.0, "D.US": -2.0}
    kept, blocked = gate_event_orders(orders, verdict, positions)
    # the sell of a long and the cover of a short are closes; a short sale opens
    assert [o.ticker for o in kept] == ["B.US", "D.US"]
    assert [o.ticker for o in blocked] == ["A.US", "C.US"]


def test_the_kill_switch_is_seen_on_the_next_event(state):
    assert event_verdict(state, PF, now=NOW).mode is None
    kill, _ = trip_halt(
        state, "kill", reason="stop", actor="user:usr_owner", scope="global", halt="all"
    )
    verdict = event_verdict(state, PF, now=NOW + timedelta(seconds=1))
    assert verdict.mode == "all" and verdict.cancel_working
    assert kill.id in verdict.halt_ids
    orders = [buy("A.US", 1, 1), sell("B.US", 1, 2)]
    assert gate_event_orders(orders, verdict, {"B.US": 1.0}) == ([], orders)
    clear_halt(state, kill.id, actor="user:usr_owner", reason="resumed")
    assert event_verdict(state, PF, now=NOW + timedelta(seconds=2)).mode is None


def test_a_buys_kill_switch_does_not_cancel_working_orders(state):
    trip_halt(state, "kill", reason="no buys", actor="user:usr_owner", scope="global")
    verdict = event_verdict(state, PF, now=NOW)
    assert verdict.mode == "buys" and not verdict.cancel_working


def test_the_owner_and_the_parent_portfolio_halts_count(state):
    trip_halt(state, "kill", reason="user stop", actor="u", scope="user", user_id="usr_a")
    assert event_verdict(state, "pf_x", now=NOW, owner_id="usr_a").mode == "buys"
    assert event_verdict(state, "pf_x", now=NOW, owner_id="usr_b").mode is None
    trip_halt(state, "runaway", reason="burst", actor="system", portfolio_id="pf_live")
    verdict = event_verdict(state, "pf_x", now=NOW, parent_portfolio_id="pf_live")
    assert verdict.mode == "buys"


def test_an_old_state_file_without_halts_blocks_nothing(tmp_path):
    bare = SqliteState(tmp_path / "bare.sqlite")
    assert event_verdict(bare, PF, now=NOW).mode is None
    bare.close()


# ---- trips ---------------------------------------------------------------------------


def _ctx(value: float, peak: float, **settings):
    pol = policy(intraday_loss_limit=settings)
    marks = ((NOW - timedelta(minutes=1), peak),)
    return context(
        Portfolio(cash=value),
        {},
        pol,
        as_of=NOW.date(),
        intraday=IntradayContext(now=NOW, equity_marks=marks),
    )


def test_a_soft_breach_opens_a_buys_halt_once(state):
    sent = []
    ctx = _ctx(9_700.0, 10_000.0, max_loss=0.02)
    halt = trip_intraday_loss(state, PF, ctx, publish=sent.append)
    assert halt is not None and halt.kind == "intraday_loss" and halt.halt == "buys"
    assert "3.00%" in halt.reason
    again = trip_intraday_loss(state, PF, ctx, publish=sent.append)
    assert again is not None and again.id == halt.id
    assert len(sent) == 1  # one alert per trip


def test_no_breach_opens_nothing(state):
    assert trip_intraday_loss(state, PF, _ctx(9_900.0, 10_000.0, max_loss=0.02)) is None
    assert active_halts(state, NOW.date(), portfolio_id=PF) == []


def test_a_hard_breach_escalates_an_open_buys_halt_to_all(state):
    soft = trip_intraday_loss(state, PF, _ctx(9_700.0, 10_000.0, max_loss=0.02), notify=False)
    hard_ctx = _ctx(9_000.0, 10_000.0, max_loss=0.02, hard_loss=0.05)
    hard = trip_intraday_loss(state, PF, hard_ctx, notify=False)
    assert soft is not None and hard is not None
    assert hard.halt == "all" and hard.id != soft.id
    [open_] = active_halts(state, NOW.date(), portfolio_id=PF)
    assert open_.id == hard.id


def test_a_hard_breach_with_flatten_keeps_closes_open(state):
    ctx = _ctx(9_000.0, 10_000.0, hard_loss=0.05, flatten=True)
    halt = trip_intraday_loss(state, PF, ctx, notify=False)
    assert halt is not None and halt.halt == "buys"


def test_an_order_rate_burst_opens_a_runaway_halt(state):
    adj = RiskAdjustment("A.US", "buy", "intraday_order_rate", 5, 0, "12 opening orders over")
    other = RiskAdjustment("B.US", "buy", "intraday_stale_data", 5, 0, "stale")
    assert trip_intraday_runaway(state, PF, [other], on=NOW.date(), notify=False) is None
    halt = trip_intraday_runaway(state, PF, [other, adj], on=NOW.date(), notify=False)
    assert halt is not None and halt.kind == "runaway" and halt.halt == "buys"
    assert "12 opening orders" in halt.reason

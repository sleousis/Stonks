"""The owner's live allocation and the runaway halt (roadmap 19.6)."""

from __future__ import annotations

import json
from datetime import date

import pytest

from stonks.core.types import Order, Portfolio
from stonks.production.halts import active_halts
from stonks.production.hooks import PortfolioHookContext
from stonks.production.hooks.live_runaway import LiveRunaway
from stonks.production.live.allocation import AllocationError, get_allocation, set_allocation
from stonks.production.live.runaway import trip_runaway
from stonks.production.rules import RiskAdjustment

AS_OF = date(2026, 9, 28)


def test_no_allocation_until_the_owner_sets_one(state):
    assert get_allocation(state, "pf_default") is None


def test_setting_the_allocation_is_audited_with_the_old_amount(state):
    set_allocation(
        state, "pf_default", 5_000, currency="usd", actor="user:usr_owner", reason="start"
    )
    got = set_allocation(
        state, "pf_default", 7_500.0, currency="USD", actor="user:usr_owner", reason="more"
    )
    assert (got.amount, got.currency, got.updated_by) == (7_500.0, "USD", "user:usr_owner")
    rows = state.sql(
        "SELECT actor, action, details_json FROM audit_log WHERE action = 'live.allocation_set'"
        " ORDER BY id"
    )
    assert len(rows) == 2
    details = json.loads(rows[1]["details_json"])
    assert details == {"amount": 7500.0, "currency": "USD", "previous": 5000.0, "reason": "more"}


@pytest.mark.parametrize(
    ("amount", "currency", "reason"),
    [(-1.0, "USD", "x"), (float("nan"), "USD", "x"), (1.0, "US", "x"), (1.0, "USD", " ")],
)
def test_bad_allocation_requests_are_refused(state, amount, currency, reason):
    with pytest.raises(AllocationError):
        set_allocation(
            state, "pf_default", amount, currency=currency, actor="user:u", reason=reason
        )
    assert get_allocation(state, "pf_default") is None


def _runaway() -> RiskAdjustment:
    return RiskAdjustment(
        ticker="A.US",
        side="sell",
        rule="runaway",
        original_quantity=1.0,
        adjusted_quantity=1.0,
        reason="runaway: 31 closing orders over the ceiling 30",
    )


def test_trip_runaway_opens_one_buys_halt(state):
    sent: list = []
    halt = trip_runaway(state, "pf_default", [_runaway()], on=AS_OF, notify=False)
    assert halt is not None and halt.kind == "runaway" and halt.halt == "buys"
    again = trip_runaway(state, "pf_default", [_runaway()], on=AS_OF, notify=False)
    assert again is not None and again.id == halt.id
    assert trip_runaway(state, "pf_default", [], on=AS_OF) is None
    assert not sent


def test_the_hook_trips_the_halt_from_the_pipeline(state, monkeypatch):
    monkeypatch.setattr("stonks.production.halts.notify_trip", lambda *a, **k: None)
    monkeypatch.setattr("stonks.production.live.runaway.notify_trip", lambda *a, **k: None)

    class _Pipeline:
        adjustments = [_runaway()]

    order = Order(client_id="c", ticker="A.US", side="sell", quantity=1.0)
    ctx = PortfolioHookContext(
        state=state,
        tick_id="t1",
        as_of=AS_OF,
        portfolio_id="pf_default",
        portfolio=Portfolio(cash=0.0),
        prices={},
        pipeline=_Pipeline(),  # type: ignore[arg-type]
        outcomes=[(order, "pending", None)],
    )
    out = LiveRunaway().run(ctx)
    assert out is not None and "pf_default" in out["runaway_halts"]
    kinds = [h.kind for h in active_halts(state, AS_OF, portfolio_id="pf_default")]
    assert kinds == ["runaway"]

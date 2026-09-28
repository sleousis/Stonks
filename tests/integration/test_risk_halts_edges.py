"""The halt store at its edges (BL-28, TO-07): bad scopes, kinds and
modes, escalating a halt that is cleared or already stops everything, and
a state DB without the ``risk_halts`` table."""

from __future__ import annotations

from datetime import date

import pytest

from stonks.config import HealthConfig
from stonks.production.halts import (
    HaltError,
    active_halts,
    escalate_halt,
    halt_health_check,
    list_halts,
    read_health,
    run_health,
    trip_halt,
)

DAY = date(2025, 6, 10)


def trip(state, **kw):
    base = {"reason": "r", "actor": "ops", "portfolio_id": "pf_default", "on": DAY}
    return trip_halt(state, kw.pop("kind", "kill"), **(base | kw))


@pytest.mark.parametrize(
    ("kw", "message"),
    [
        ({"scope": "planet"}, "unknown halt scope 'planet'"),
        ({"kind": "whim"}, "unknown halt kind 'whim'"),
        ({"halt": "sells"}, "halt must be 'buys' or 'all', got 'sells'"),
    ],
)
def test_a_halt_needs_a_known_scope_kind_and_mode(state, kw, message):
    with pytest.raises(HaltError, match=message):
        trip(state, **kw)
    assert list_halts(state, include_cleared=True) == []


def test_escalating_an_all_halt_returns_it_unchanged(state):
    halt, _ = trip(state, halt="all")
    assert escalate_halt(state, halt.id, actor="ops", reason="again") == halt
    assert [h.id for h in active_halts(state, DAY, portfolio_id="pf_default")] == [halt.id]


def test_a_cleared_halt_cannot_be_escalated(state):
    from stonks.production.halts import clear_halt

    halt, _ = trip(state)
    clear_halt(state, halt.id, actor="ops", reason="done")
    with pytest.raises(HaltError, match=f"halt {halt.id} was already cleared"):
        escalate_halt(state, halt.id, actor="ops", reason="late")


# ---- without the table ------------------------------------------------------------------


@pytest.fixture
def old_state(state):
    """A state DB from before migration 016 (reports point at halts)."""
    for table in ("reconcile_reports", "risk_halts"):
        state.execute(f"DROP TABLE {table}")
    return state


def test_no_table_means_no_halt_in_force(old_state):
    assert active_halts(old_state, DAY, portfolio_id="pf_default") == []
    check = halt_health_check(old_state, DAY)
    assert check.ok and check.detail == "no risk_halts table"


def test_health_without_the_table_is_the_plain_check(old_state, lake):
    ran = run_health(old_state, lake, [], HealthConfig())
    read = read_health(old_state, lake, [], HealthConfig())
    assert "risk_halts" not in {c.name for c in ran.checks}
    assert {c.name for c in ran.checks} == {c.name for c in read.checks}

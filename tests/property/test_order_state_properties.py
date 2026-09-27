"""The order state machine (roadmap 19.1): no sequence of attempted
changes can take an order through a transition outside the table, a
terminal order never changes, and only ``pending`` may be submitted."""

from __future__ import annotations

from hypothesis import given
from hypothesis import strategies as st

from stonks.execution.order_state import (
    ORDER_STATES,
    TERMINAL,
    TRANSITIONS,
    IllegalTransitionError,
    can_transition,
    check_transition,
    ledger_status,
    may_submit,
)

states = st.sampled_from(ORDER_STATES)


@given(st.lists(states, max_size=30))
def test_no_illegal_transition_can_happen(attempts):
    current = "pending"
    history = [current]
    for new in attempts:
        try:
            check_transition(current, new)
        except IllegalTransitionError:
            assert new != current and new not in TRANSITIONS[current]
            continue
        assert new == current or new in TRANSITIONS[current]
        if current in TERMINAL:
            assert new == current
        current = new
        history.append(current)
    # once terminal, always the same terminal state
    for i, s in enumerate(history):
        if s in TERMINAL:
            assert set(history[i:]) == {s}


@given(states, states)
def test_the_ledger_status_follows_the_state(a, b):
    assert ledger_status(a) in ("pending", "partially_filled", "filled", "cancelled", "rejected")
    if can_transition(a, b) and a in TERMINAL:
        assert a == b


def test_every_state_has_a_row_and_unknown_resolves_to_a_broker_state():
    assert set(TRANSITIONS) == set(ORDER_STATES)
    assert "pending" not in TRANSITIONS["unknown"]
    assert "unknown" not in TRANSITIONS["unknown"]
    assert all(not TRANSITIONS[t] for t in TERMINAL)


def test_only_pending_may_be_submitted():
    assert may_submit("pending")
    assert not any(may_submit(s) for s in ORDER_STATES if s != "pending")
    assert not may_submit(None)

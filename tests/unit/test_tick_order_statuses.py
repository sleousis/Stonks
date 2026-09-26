"""The booked-status read-back after an external submit (TT-02).

Pyright found the tick storing a plain ``str`` where an ``OrderStatus`` is
declared. The read-back now keeps only known statuses, so an odd value in
the ledger falls back to what the submission reported.
"""

from __future__ import annotations

from stonks.production.tick import _order_statuses


class _FakeState:
    def __init__(self, rows):
        self.rows = rows
        self.queries: list[tuple[str, list]] = []

    def sql(self, query, params):
        self.queries.append((query, params))
        return self.rows


def test_known_statuses_are_returned_by_client_id():
    state = _FakeState(
        [{"client_id": "a", "status": "rejected"}, {"client_id": "b", "status": "filled"}]
    )
    assert _order_statuses(state, ["a", "b", None]) == {"a": "rejected", "b": "filled"}
    assert state.queries[0][1] == ["a", "b"]


def test_an_unknown_status_is_left_out():
    state = _FakeState([{"client_id": "a", "status": "accepted"}])
    assert _order_statuses(state, ["a"]) == {}


def test_no_ids_skips_the_query():
    state = _FakeState([])
    assert _order_statuses(state, [None]) == {}
    assert state.queries == []

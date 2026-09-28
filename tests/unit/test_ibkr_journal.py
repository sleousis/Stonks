"""The broker event journal and its replay (roadmap 23.15).

Every call the broker makes to the gateway is written to disk with its raw
answer or its error. A journal read back through ``ReplayIbClient`` answers
the same calls the same way, so an incident can be replayed in a test."""

from __future__ import annotations

import json
from datetime import timedelta

import pytest

from stonks.core.clock import FakeClock
from stonks.core.types import Order
from stonks.execution.brokers.base import OrderRejectedError
from stonks.execution.brokers.ibkr.broker import IbkrBroker
from stonks.execution.brokers.ibkr.client import IbApiError, IbClient, IbConnectionError
from stonks.execution.brokers.ibkr.journal import (
    FileJournal,
    JournalingIbClient,
    MemoryJournal,
    ReplayIbClient,
    read_journal,
    redact_account,
)
from tests.fakes.ib_gateway import T0, FakeIbGateway

ACCOUNT = "DU1234567"


def buy(client_id: str = "t1-s1-AAPL.US-buy", **kw) -> Order:
    base = {"client_id": client_id, "ticker": "AAPL.US", "side": "buy", "quantity": 10.0,
            "decision_price": 200.0}  # fmt: skip
    base.update(kw)
    return Order(**base)


def gateway() -> FakeIbGateway:
    gw = FakeIbGateway()
    gw.set_values(NetLiquidation="100000", TotalCashValue="50000", AccountCode=ACCOUNT)
    return gw


def recorded(gw: FakeIbGateway | None = None):
    gw = gw or gateway()
    journal = MemoryJournal()
    client = JournalingIbClient(gw, journal, clock=FakeClock(T0))
    return IbkrBroker(client, mode="paper"), gw, journal


def test_the_journaling_client_is_an_ib_client():
    client = JournalingIbClient(FakeIbGateway(), MemoryJournal())
    assert isinstance(client, IbClient)


def test_order_status_executions_and_errors_are_recorded():
    broker, gw, journal = recorded()
    broker.place_order(buy())
    gw.fill("t1-s1-AAPL.US-buy", 10, 201.0, commission=1.0)
    broker.reconcile()
    gw.reject_next = (201, "Order rejected - reason: insufficient funds")
    with pytest.raises(OrderRejectedError):
        broker.place_order(buy("t2-s1-AAPL.US-buy"))
    kinds = {e.kind for e in journal.events}
    assert {"order_status", "execution", "error"} <= kinds
    error = next(e for e in journal.events if e.kind == "error")
    assert error.method == "place_order"
    assert error.error is not None
    assert error.error["type"] == "IbApiError" and error.error["code"] == 201
    assert error.error["message"] == "Order rejected - reason: insufficient funds"
    assert isinstance(error.error["req_id"], int)
    seqs = [e.seq for e in journal.events]
    assert seqs == sorted(seqs) and len(set(seqs)) == len(seqs)


def test_account_ids_never_reach_the_journal():
    broker, gw, journal = recorded()
    broker.place_order(buy())
    broker.fetch_portfolio()
    text = "\n".join(json.dumps(e.as_dict()) for e in journal.events)
    assert ACCOUNT not in text
    assert redact_account(ACCOUNT) in text
    assert redact_account(ACCOUNT).startswith("DU")  # a paper account stays a paper account
    assert redact_account("U7654321").startswith("U") and redact_account(ACCOUNT) != ACCOUNT


def test_unchanged_answers_are_stored_once():
    broker, _, journal = recorded()
    broker.ensure_ready()
    for _ in range(3):
        broker.open_orders()
    polls = [e for e in journal.events if e.method == "all_open_trades"] or [
        e for e in journal.events if e.method == "open_trades"
    ]
    assert len(polls) == 3
    assert [p.unchanged for p in polls] == [False, True, True]


def test_a_file_journal_writes_one_json_line_per_event_and_reads_back(tmp_path):
    clock = FakeClock(T0)
    journal = FileJournal(tmp_path, clock=clock)
    client = JournalingIbClient(gateway(), journal, clock=clock)
    broker = IbkrBroker(client, mode="paper")
    broker.place_order(buy())
    clock.advance(timedelta(days=1))
    broker.fetch_portfolio()
    files = sorted(tmp_path.glob("*.jsonl"))
    assert [f.name for f in files] == [
        f"{T0.date()}.jsonl",
        f"{(T0 + timedelta(days=1)).date()}.jsonl",
    ]
    first = files[0].read_text(encoding="utf-8").splitlines()
    assert all(json.loads(line)["method"] for line in first)
    events = read_journal(files)
    assert len(events) == len(first) + len(files[1].read_text(encoding="utf-8").splitlines())


def test_replay_answers_the_same_calls_the_same_way(tmp_path):
    """An incident: a fill, then the gateway rejects the next order, then
    drops the socket. Replayed from disk, the broker sees the same."""
    clock = FakeClock(T0)
    journal = FileJournal(tmp_path, clock=clock)
    gw = gateway()
    live = IbkrBroker(JournalingIbClient(gw, journal, clock=clock), mode="paper")

    def incident(broker: IbkrBroker, gateway: FakeIbGateway | None) -> list[object]:
        seen: list[object] = []
        broker.place_order(buy())
        if gateway is not None:
            gateway.fill("t1-s1-AAPL.US-buy", 10, 201.0, commission=1.0)
        seen.append([(f.ticker, f.quantity, f.price) for f in broker.reconcile()])
        state = broker.get_order_state("t1-s1-AAPL.US-buy")
        seen.append(state.state if state else None)
        if gateway is not None:
            gateway.reject_next = (201, "Order rejected - reason: insufficient funds")
        try:
            broker.place_order(buy("t2-s1-AAPL.US-buy"))
        except OrderRejectedError as exc:
            seen.append(type(exc).__name__)
        seen.append(broker.fetch_portfolio().positions)
        return seen

    original = incident(live, gw)
    replay = ReplayIbClient.from_files(sorted(tmp_path.glob("*.jsonl")))
    replayed = incident(IbkrBroker(replay, mode="paper"), None)
    assert replayed == original
    assert original[0] == [("AAPL.US", 10.0, 201.0)]


def test_replay_raises_recorded_connection_errors_and_runs_dry():
    gw = FakeIbGateway()
    journal = MemoryJournal()
    client = JournalingIbClient(gw, journal)
    client.connect()
    gw.drop()
    with pytest.raises(IbConnectionError):
        client.positions(ACCOUNT)
    replay = ReplayIbClient(journal.events)
    replay.connect()
    with pytest.raises(IbConnectionError):
        replay.positions("anything")
    with pytest.raises(IbConnectionError, match="no more"):
        replay.positions("anything")


def test_replay_keeps_api_error_codes():
    class Rejecting(FakeIbGateway):
        def cancel_order(self, order_id: int) -> None:
            raise IbApiError(10147, "OrderId 7 that needs to be cancelled is not found.")

    journal = MemoryJournal()
    client = JournalingIbClient(Rejecting(), journal)
    with pytest.raises(IbApiError):
        client.cancel_order(7)
    with pytest.raises(IbApiError) as info:
        ReplayIbClient(journal.events).cancel_order(7)
    assert info.value.code == 10147

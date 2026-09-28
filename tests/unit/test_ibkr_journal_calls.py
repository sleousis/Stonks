"""The broker event journal, call by call (roadmap 23.15).

Every IbClient call is recorded and replayed with its answer, the optional
calls pass through only when the wrapped client has them, a broken journal
never breaks trading, and the summary reads each kind of event."""

from __future__ import annotations

import dataclasses
import json
from datetime import UTC, datetime

import pytest

from stonks.execution.brokers.ibkr.client import (
    IbAccountValue,
    IbApiError,
    IbConnectionError,
    IbContract,
    IbContractDetails,
    IbContractQuery,
    IbExecution,
    IbLinkStatus,
    IbOrderRequest,
    IbShortability,
    IbSnapshot,
    IbTrade,
    IbWhatIf,
)
from stonks.execution.brokers.ibkr.journal import (
    EventJournal,
    JournalEvent,
    JournalingIbClient,
    MemoryJournal,
    ReplayIbClient,
    decode,
    encode,
    journal_summary,
    read_journal,
    redact_account,
)

T0 = datetime(2026, 9, 28, 13, 30, tzinfo=UTC)
ACCOUNT = "DU1234567"
AAPL = IbContract(con_id=265598, symbol="AAPL", sec_type="STK", currency="USD")
ORDER = IbOrderRequest(
    action="BUY",
    total_quantity=10,
    order_type="LMT",
    tif="DAY",
    order_ref="t1-s1-AAPL.US-buy",
    account=ACCOUNT,
    limit_price=200.0,
)
TRADE = IbTrade(
    order_id=7,
    perm_id=70,
    order_ref="t1-s1-AAPL.US-buy",
    contract=AAPL,
    action="BUY",
    total_quantity=10,
    status="Submitted",
    account=ACCOUNT,
)
EXEC = IbExecution(
    exec_id="e1",
    order_ref="t1-s1-AAPL.US-buy",
    perm_id=70,
    contract=AAPL,
    side="BOT",
    shares=10,
    price=201.0,
    time=T0,
    account=ACCOUNT,
)


class Stub:
    """A gateway client with canned answers for every call."""

    def __init__(self) -> None:
        self.closed = False

    def connect(self) -> None:
        return None

    def disconnect(self) -> None:
        return None

    def status(self) -> IbLinkStatus:
        return IbLinkStatus(connected=True, last_error=(1100, "lost"))

    def managed_accounts(self) -> list[str]:
        return [ACCOUNT]

    def server_time(self) -> datetime:
        return T0

    def contract_details(self, query: IbContractQuery) -> list[IbContractDetails]:
        return [IbContractDetails(contract=AAPL, min_tick=0.01)]

    def place_order(self, contract: IbContract, order: IbOrderRequest) -> IbTrade:
        return TRADE

    def cancel_order(self, order_id: int) -> None:
        return None

    def global_cancel(self) -> None:
        return None

    def open_trades(self) -> list[IbTrade]:
        return [TRADE]

    def all_open_trades(self) -> list[IbTrade]:
        return [TRADE]

    def completed_trades(self) -> tuple[IbTrade, ...]:
        return ()

    def executions(self) -> list[IbExecution]:
        return [EXEC]

    def positions(self, account: str) -> list[object]:
        return []

    def account_values(self, account: str) -> list[IbAccountValue]:
        return [
            IbAccountValue(account=account, tag="AccountCode", value=account),
            IbAccountValue(account=account, tag="NetLiquidation", value="100000", currency="USD"),
        ]

    def what_if(self, contract: IbContract, order: IbOrderRequest) -> IbWhatIf:
        return IbWhatIf(
            init_margin_change=1.0,
            maint_margin_change=1.0,
            equity_with_loan_after=99.0,
            commission=1.0,
        )

    def snapshots(self, contracts: list[IbContract]) -> list[IbSnapshot]:
        return [
            IbSnapshot(con_id=c.con_id, last=200.0, bid=199.9, ask=200.1, time=T0)
            for c in contracts
        ]

    def shortability(self, contracts: list[IbContract]) -> list[IbShortability]:
        return [IbShortability(con_id=c.con_id, indicator=3.0, shares=1e6) for c in contracts]

    def close(self) -> None:
        self.closed = True


def every_call(client) -> list[object]:
    """Each IbClient call once, answers in order."""
    client.connect()
    out: list[object] = [
        client.status(),
        list(client.managed_accounts()),
        client.server_time(),
        list(client.contract_details(IbContractQuery(symbol="AAPL", currency="USD"))),
        client.place_order(AAPL, ORDER),
    ]
    client.cancel_order(7)
    client.global_cancel()
    out += [
        list(client.open_trades()),
        list(client.all_open_trades()),
        list(client.completed_trades()),
        list(client.executions()),
        list(client.positions(ACCOUNT)),
        [(v.tag, v.value) for v in client.account_values(ACCOUNT)],
        client.what_if(AAPL, ORDER),
        list(client.snapshots([AAPL])),
        list(client.shortability([AAPL])),
    ]
    client.disconnect()
    return out


def test_every_call_is_recorded_and_replays_the_same():
    journal = MemoryJournal()
    live = every_call(JournalingIbClient(Stub(), journal))
    methods = [e.method for e in journal.events]
    assert methods[0] == "connect" and methods[-1] == "disconnect"
    assert "shortability" in methods and "global_cancel" in methods
    replay = ReplayIbClient(journal.events)
    replayed = every_call(replay)
    # the account ids come back redacted, everything else as it was
    redacted = redact_account(ACCOUNT)
    assert replayed[1] == [redacted]
    assert replayed[10] == [("AccountCode", redacted), ("NetLiquidation", "100000")]
    assert replayed[4] == dataclasses.replace(TRADE, account=redacted)
    for i in (0, 2, 3, 7, 11, 12, 13):
        assert replayed[i] == live[i]
    assert replay.remaining() == {}


def test_no_account_id_reaches_the_journal():
    journal = MemoryJournal()
    every_call(JournalingIbClient(Stub(), journal))
    text = json.dumps([e.as_dict() for e in journal.events])
    assert ACCOUNT not in text


def test_close_reaches_the_wrapped_client_only_when_it_has_one():
    stub = Stub()
    JournalingIbClient(stub, MemoryJournal()).close()
    assert stub.closed

    class NoClose:
        pass

    JournalingIbClient(NoClose(), MemoryJournal()).close()  # nothing to close, no error


def test_optional_calls_pass_through_only_when_the_client_has_them():
    client = JournalingIbClient(Stub(), MemoryJournal())
    assert client.inner.__class__ is Stub
    assert callable(client.shortability)
    with pytest.raises(AttributeError):
        client.option_params  # the stub has no option data  # noqa: B018
    with pytest.raises(AttributeError):
        client.not_a_call  # noqa: B018
    with pytest.raises(AttributeError):
        client._private  # noqa: B018


def test_replay_offers_an_optional_call_only_when_it_was_recorded():
    replay = ReplayIbClient([])
    with pytest.raises(AttributeError):
        replay.shortability  # noqa: B018
    with pytest.raises(AttributeError):
        replay.not_a_call  # noqa: B018
    replay.disconnect()  # nothing recorded: a no-op, never an error
    with pytest.raises(IbConnectionError, match="no more status"):
        replay.status()


def test_a_broken_journal_never_breaks_trading():
    class Broken(EventJournal):
        def append(self, event: JournalEvent) -> None:
            raise OSError("disk full")

    client = JournalingIbClient(Stub(), Broken())
    assert client.place_order(AAPL, ORDER) == TRADE


def test_the_base_journal_must_be_implemented():
    with pytest.raises(NotImplementedError):
        EventJournal().append(JournalEvent(1, T0.isoformat(), "call", "connect"))


@pytest.mark.parametrize(
    ("error", "raised"),
    [
        (TimeoutError("slow gateway"), TimeoutError),
        (IbConnectionError("socket closed"), IbConnectionError),
        (KeyError("odd"), RuntimeError),
    ],
)
def test_replay_raises_what_was_recorded(error, raised):
    class Failing(Stub):
        def server_time(self) -> datetime:
            raise error

    journal = MemoryJournal()
    with pytest.raises(type(error)):
        JournalingIbClient(Failing(), journal).server_time()
    with pytest.raises(raised):
        ReplayIbClient(journal.events).server_time()


def test_read_journal_skips_blank_and_broken_lines(tmp_path):
    good = JournalEvent(1, T0.isoformat(), "call", "connect").as_dict()
    path = tmp_path / "day.jsonl"
    path.write_text(
        "\n".join(["", json.dumps(good), "not json", json.dumps({"seq": 2}), "  "]),
        encoding="utf-8",
    )
    events = read_journal([path])
    assert [e.method for e in events] == ["connect"]


def test_encode_and_decode_round_trip_plain_values():
    value = {"when": T0, "pair": (1, "a"), "nested": {"x": [AAPL]}}
    assert decode(encode(value)) == value
    with pytest.raises(ValueError, match="unknown journal type"):
        decode({"__type__": "NotAType"})


def test_redact_keeps_an_empty_account_empty():
    assert redact_account("") == ""
    assert redact_account("1234").startswith("x")


def test_summary_reads_each_kind_of_event():
    journal = MemoryJournal()
    client = JournalingIbClient(Stub(), journal)
    client.all_open_trades()
    client.all_open_trades()
    client.executions()
    client.status()

    class Rejecting(Stub):
        def cancel_order(self, order_id: int) -> None:
            raise IbApiError(10147, "not found")

        def global_cancel(self) -> None:
            raise IbConnectionError("socket closed")

    failing = JournalingIbClient(Rejecting(), journal)
    with pytest.raises(IbApiError):
        failing.cancel_order(7)
    with pytest.raises(IbConnectionError):
        failing.global_cancel()
    journal.events.append(
        JournalEvent(99, T0.isoformat(), "order_status", "place_order", result=None)
    )
    rows = journal_summary(journal.events)
    details = [r["detail"] for r in rows]
    assert details[0] == "t1-s1-AAPL.US-buy Submitted 0.0/10"
    assert details[1] == "unchanged"
    assert details[2] == "e1 BOT 10 @ 201.0"
    assert details[3] == ""
    assert details[4] == "IbApiError 10147: not found"
    assert details[5] == "IbConnectionError: socket closed"
    assert details[6] == ""  # an order event without a body
    assert rows[0]["kind"] == "order_status" and rows[2]["kind"] == "execution"

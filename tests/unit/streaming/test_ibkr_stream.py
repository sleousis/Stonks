"""Live prices through the IBKR adapter's quotes (roadmap 21.1)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from stonks.config import Settings
from stonks.core.clock import FakeClock
from stonks.core.stream import Heartbeat, QuoteTick
from stonks.execution.brokers.base import (
    BrokerError,
    BrokerUnavailableError,
    LiveTradingRefusedError,
    Quote,
)
from stonks.execution.brokers.ibkr.broker import IbkrBroker
from stonks.execution.brokers.ibkr.client import IbSnapshot
from stonks.execution.brokers.ibkr.factory import endpoint_for, pick_gateway
from stonks.execution.brokers.ibkr.settings import IbkrBrokerConfig
from stonks.streaming.base import StreamAuthError, StreamContext, StreamDisconnectedError
from stonks.streaming.registry import build_stream_source, stream_source_classes
from stonks.streaming.sources.ibkr import IbkrQuoteStream
from tests.fakes.ib_gateway import AAPL, MSFT, T0, FakeIbGateway


class ScriptedQuotes:
    """A QuoteSource answering one scripted reply per call."""

    def __init__(self, replies):
        self.replies = list(replies)
        self.calls: list[list[str]] = []

    def quotes(self, tickers):
        self.calls.append(list(tickers))
        reply = self.replies.pop(0) if self.replies else {}
        if isinstance(reply, BaseException):
            raise reply
        return reply


def q(ticker: str, last: float | None, seconds: float = 0, delayed: bool = False) -> Quote:
    return Quote(ticker, last, None, None, T0 + timedelta(seconds=seconds), delayed)


def run(src: IbkrQuoteStream, tickers: list[str], polls: int) -> list:
    out = []
    beats = 0
    for ev in src.stream(tickers):
        out.append(ev)
        if isinstance(ev, Heartbeat):
            beats += 1
            if beats >= polls:
                src.close()
    return out


def test_is_registered():
    assert stream_source_classes()["ibkr"] is IbkrQuoteStream


def test_yields_changed_quotes_and_a_heartbeat_per_poll():
    quotes = ScriptedQuotes(
        [
            {"AAPL.US": q("AAPL.US", 200.0), "MSFT.US": q("MSFT.US", 400.0, delayed=True)},
            {"AAPL.US": q("AAPL.US", 200.0), "MSFT.US": q("MSFT.US", 401.0, 5)},
        ]
    )
    waits: list[float] = []
    clock = FakeClock(T0)
    src = IbkrQuoteStream(quotes, poll_seconds=5, clock=clock, sleep=waits.append)
    got = run(src, ["AAPL.US", "MSFT.US"], polls=2)
    data = [e for e in got if isinstance(e, QuoteTick)]
    assert [(e.ticker, e.last) for e in data] == [
        ("AAPL.US", 200.0),
        ("MSFT.US", 400.0),
        ("MSFT.US", 401.0),
    ]
    assert data[1].delayed and data[0].source == "ibkr"
    assert waits == [5, 5]
    assert sum(isinstance(e, Heartbeat) for e in got) == 2


def test_naive_quote_times_are_read_as_utc():
    naive = Quote("AAPL.US", 1.0, None, None, datetime(2026, 9, 28, 13, 30), False)
    src = IbkrQuoteStream(ScriptedQuotes([{"AAPL.US": naive}]), sleep=lambda s: None)
    (tick, _) = run(src, ["AAPL.US"], polls=1)
    assert tick.timestamp == datetime(2026, 9, 28, 13, 30, tzinfo=UTC)


@pytest.mark.parametrize(
    "error", [BrokerUnavailableError("down"), ConnectionError("gone"), TimeoutError()]
)
def test_an_outage_is_a_disconnect(error):
    src = IbkrQuoteStream(ScriptedQuotes([error]), sleep=lambda s: None)
    with pytest.raises(StreamDisconnectedError):
        next(src.stream(["AAPL.US"]))


def test_other_broker_errors_are_disconnects_too():
    src = IbkrQuoteStream(ScriptedQuotes([BrokerError("odd")]), sleep=lambda s: None)
    with pytest.raises(StreamDisconnectedError, match="odd"):
        next(src.stream(["AAPL.US"]))


def test_a_refused_account_is_never_retried():
    src = IbkrQuoteStream(ScriptedQuotes([LiveTradingRefusedError("wrong account")]))
    with pytest.raises(StreamAuthError, match="wrong account"):
        next(src.stream(["AAPL.US"]))


def test_over_the_fake_gateway():
    gw = FakeIbGateway()
    gw.snapshot_data = {
        AAPL.contract.con_id: IbSnapshot(AAPL.contract.con_id, 201.0, 200.9, 201.1, T0, 1),
        MSFT.contract.con_id: IbSnapshot(MSFT.contract.con_id, float("nan"), 400.0, 400.2, T0, 3),
    }
    broker = IbkrBroker(gw, mode="paper")
    src = IbkrQuoteStream(broker, sleep=lambda s: None)
    got = [e for e in run(src, ["AAPL.US", "MSFT.US"], polls=1) if isinstance(e, QuoteTick)]
    by = {e.ticker: e for e in got}
    assert by["AAPL.US"].reference == 201.0
    assert by["MSFT.US"].delayed and by["MSFT.US"].reference == pytest.approx(400.1)


def test_the_stream_role_has_its_own_client_id():
    cfg = IbkrBrokerConfig.model_validate(
        {"gateways": {"paper": {"host": "gw", "port": 4004, "mode": "paper"}}}
    )
    _, gw = pick_gateway(cfg)
    assert endpoint_for(cfg, gw, "stream").client_id == 14
    assert endpoint_for(cfg, gw, "stream").readonly


def test_builds_from_settings_through_the_adapter_factory(monkeypatch):
    built = {}

    def fake_connect(config, **kw):
        built.update(kw)
        return ScriptedQuotes([])

    monkeypatch.setattr("stonks.streaming.sources.ibkr.connect_ibkr", fake_connect)
    settings = Settings()
    settings.streaming.ibkr.gateway = "paper"
    settings.streaming.ibkr.poll_seconds = 2.0
    src = build_stream_source(StreamContext(settings=settings), "ibkr")
    assert isinstance(src, IbkrQuoteStream) and src.poll_seconds == 2.0
    assert built["role"] == "stream" and built["gateway"] == "paper"

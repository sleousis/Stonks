"""The live stage guard (roadmap 19.9): at a broker that trades real money,
an order that may open needs the portfolio at ``live_small`` or higher.
Closes, covers of shorts included, always pass (P28), so a demoted book can
wind down. The guard sits on every real broker the factories build, not
only on the IBKR adapter."""

from __future__ import annotations

import pytest

from stonks.core.types import Order, Portfolio
from stonks.execution.brokers.base import LiveTradingRefusedError
from stonks.production.live.stage_guard import (
    guard_live_stage,
    order_closes,
    stage_refusal,
)


class _Broker:
    def __init__(self, *, real_money: bool | None = True) -> None:
        if real_money is not None:
            self.real_money = real_money
        self.sent: list[str] = []

    def fetch_portfolio(self) -> Portfolio:
        return Portfolio(cash=0.0)

    def place_order(self, order: Order) -> None:
        self.sent.append(order.client_id)

    def reconcile(self) -> list:
        return []


def _order(cid: str, side: str, effect: str | None = None) -> Order:
    return Order(
        client_id=cid,
        ticker="AAA.US",
        side=side,
        quantity=1.0,
        position_effect=effect,  # type: ignore[arg-type]
    )


def test_the_rule_lets_closes_pass_at_any_stage():
    assert order_closes(_order("a", "sell"))
    assert order_closes(_order("b", "buy", "close"))  # buy to cover
    assert not order_closes(_order("c", "sell", "open"))  # short sale
    assert not order_closes(_order("d", "buy"))
    for stage in ("sim_paper", "broker_paper", None):
        assert stage_refusal(_order("a", "sell"), stage) is None
        assert stage_refusal(_order("b", "buy", "close"), stage) is None
        assert stage_refusal(_order("d", "buy"), stage) is not None
        assert stage_refusal(_order("c", "sell", "open"), stage) is not None
    assert stage_refusal(_order("d", "buy"), "live_small") is None
    assert stage_refusal(_order("d", "buy"), "live_scale") is None


def test_a_real_money_broker_below_live_small_sends_only_closes():
    broker = _Broker()
    guarded = guard_live_stage(broker, lambda: "sim_paper")
    with pytest.raises(LiveTradingRefusedError, match="live_small"):
        guarded.place_order(_order("open", "buy"))
    with pytest.raises(LiveTradingRefusedError):
        guarded.place_order(_order("short", "sell", "open"))
    guarded.place_order(_order("close", "sell"))
    guarded.place_order(_order("cover", "buy", "close"))
    assert broker.sent == ["close", "cover"]
    # the same object: every capability check (isinstance) still holds
    assert guarded is broker


def test_live_small_opens_and_a_paper_account_is_not_checked():
    live = guard_live_stage(_Broker(), lambda: "live_small")
    live.place_order(_order("open", "buy"))
    paper = guard_live_stage(_Broker(real_money=False), lambda: "sim_paper")
    paper.place_order(_order("open", "buy"))
    assert live.sent == ["open"] and paper.sent == ["open"]


def test_an_unknown_broker_counts_as_real_money():
    broker = guard_live_stage(_Broker(real_money=None), lambda: "broker_paper")
    with pytest.raises(LiveTradingRefusedError):
        broker.place_order(_order("open", "buy"))


def test_an_unreadable_stage_refuses_opens_but_not_closes():
    def broken() -> str | None:
        raise RuntimeError("db gone")

    broker = guard_live_stage(_Broker(), broken)
    with pytest.raises(LiveTradingRefusedError, match="could not be read"):
        broker.place_order(_order("open", "buy"))
    broker.place_order(_order("close", "sell"))
    assert broker.sent == ["close"]


def test_guarding_twice_checks_once():
    calls: list[int] = []

    def lookup() -> str:
        calls.append(1)
        return "live_small"

    broker = guard_live_stage(guard_live_stage(_Broker(), lookup), lookup)
    broker.place_order(_order("open", "buy"))
    assert len(calls) == 1

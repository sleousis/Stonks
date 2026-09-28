"""A broker session for the API (roadmap 19.17).

Each process role has its own IBKR API client id, so the API (the kill
switch, manual orders) connects while a tick holds the tick's id. IBKR keeps
in sync only a client's own orders, unless it is the gateway's master
client, and lets only the placing client (or the master) cancel an order.
``FakeIbGateway`` models that ownership."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from stonks.core.types import Order
from stonks.execution.brokers.base import BrokerUnavailableError
from stonks.execution.brokers.ibkr.broker import IbkrBroker
from stonks.execution.brokers.ibkr.client import IbApiError, IbConnectionError
from stonks.execution.brokers.ibkr.errors import OrderOwnedElsewhereError, classify
from stonks.execution.brokers.ibkr.factory import (
    Role,
    connect_ibkr,
    endpoint_for,
    owner_sessions,
    pick_gateway,
)
from stonks.execution.brokers.ibkr.settings import IbkrBrokerConfig
from tests.fakes.ib_gateway import AAPL, FakeIbGateway

GATEWAYS = {"paper": {"host": "gw", "port": 4004, "mode": "paper", "portfolios": ["pf_default"]}}
CONFIG = IbkrBrokerConfig(gateways=GATEWAYS)
TICK_ORDER = "2026-09-28:t1:s1:AAPL.US:buy"


def limit_buy(client_id: str = TICK_ORDER) -> Order:
    return Order(client_id=client_id, ticker="AAPL.US", side="buy", quantity=10.0,
                 order_type="limit", limit_price=190.0, time_in_force="day")  # fmt: skip


def broker(gw: FakeIbGateway, role: Role, config: IbkrBrokerConfig = CONFIG) -> IbkrBroker:
    """Every broker gets its own session of the one gateway, as each
    process opens its own socket."""
    return connect_ibkr(config, role=role, client_factory=lambda ep: gw.session(ep.client_id))


@pytest.fixture
def gw() -> FakeIbGateway:
    return FakeIbGateway()


@pytest.fixture
def tick(gw) -> IbkrBroker:
    """A running tick: connected as client 11 with one working order."""
    t = broker(gw, "tick")
    t.place_order(limit_buy())
    return t


# ---- settings ---------------------------------------------------------------------------


def test_the_api_role_has_client_id_16_and_may_trade():
    assert CONFIG.client_ids.api == 16
    _, gw = pick_gateway(CONFIG)
    api = endpoint_for(CONFIG, gw, "api")
    assert (api.client_id, api.readonly) == (16, False)
    assert api.reconnect_deadline == CONFIG.reconnect_deadline_seconds


def test_the_master_client_id_defaults_to_the_ticks():
    assert CONFIG.master_client_id is None
    assert CONFIG.master_id == 11
    assert IbkrBrokerConfig(master_client_id=16).master_id == 16
    assert IbkrBrokerConfig(client_ids={"tick": 21}).master_id == 21


def test_client_ids_must_differ():
    with pytest.raises(ValidationError, match="must all differ"):
        IbkrBrokerConfig(client_ids={"api": 11})


# ---- the fake models client-id ownership --------------------------------------------------


def test_a_client_id_connects_once(gw):
    gw.connect()
    with pytest.raises(IbConnectionError, match="326"):
        gw.session(11).connect()
    other = gw.session(16)
    other.connect()
    assert other.status().connected


def test_a_client_sees_and_cancels_only_its_own_orders(gw, tick):
    trade = gw.trade(TICK_ORDER)
    assert trade.client_id == 11
    sync = gw.session(12)
    sync.connect()
    assert sync.open_trades() == []
    assert [t.order_ref for t in sync.all_open_trades()] == [TICK_ORDER]
    with pytest.raises(IbApiError) as info:
        sync.cancel_order(trade.order_id)
    assert info.value.code == 10147
    # never read as "nothing to cancel": the order is still working
    assert classify(10147) == "other"
    assert gw.trade(TICK_ORDER).status == "Submitted"


def test_the_master_sees_and_cancels_every_order():
    gw = FakeIbGateway(master_client_id=16)
    broker(gw, "tick").place_order(limit_buy())
    api = gw.session(16)
    api.connect()
    assert [t.order_ref for t in api.open_trades()] == [TICK_ORDER]
    api.cancel_order(gw.trade(TICK_ORDER).order_id)
    assert gw.trade(TICK_ORDER).status == "Cancelled"
    assert gw.cancels_by == [(16, 1)]


def test_global_cancel_reaches_every_client(gw, tick):
    other = gw.session(12)
    other.connect()
    other.global_cancel()
    assert gw.trade(TICK_ORDER).status == "Cancelled"


# ---- the API broker ---------------------------------------------------------------------


def test_the_api_connects_while_a_tick_holds_its_id(gw, tick):
    # the old wiring: a second broker on the tick's id cannot connect
    with pytest.raises(BrokerUnavailableError, match="326"):
        broker(gw, "tick").ensure_ready()
    api = broker(gw, "api")
    api.ensure_ready()
    assert (api.client_id, api.master_client_id, api.is_master) == (16, 11, False)
    assert api.portfolios == ("pf_default",)


def test_the_api_sees_the_ticks_orders_through_req_all_open_orders(gw, tick):
    api = broker(gw, "api")
    assert [o.client_id for o in api.open_orders()] == [TICK_ORDER]
    assert gw.all_open_requests >= 1
    state = api.get_order_state(TICK_ORDER)
    assert state is not None and state.quantity == 10.0
    # the tick is the master: its own view already holds every order
    before = gw.all_open_requests
    assert tick.is_master
    assert [o.client_id for o in tick.open_orders()] == [TICK_ORDER]
    assert gw.all_open_requests == before


def test_the_api_cannot_cancel_a_running_ticks_order_one_by_one(gw, tick):
    api = broker(gw, "api")
    with pytest.raises(OrderOwnedElsewhereError, match="connected now"):
        api.cancel_order(TICK_ORDER)
    assert gw.trade(TICK_ORDER).status == "Submitted"
    assert gw.sessions[-1].closed  # the owner session was given back


def test_global_cancel_works_from_the_api_while_the_tick_runs(gw, tick):
    api = broker(gw, "api")
    assert api.cancel_all() == 1
    assert gw.trade(TICK_ORDER).status == "Cancelled"
    assert gw.global_cancels == 1


def test_after_the_tick_the_api_cancels_as_the_owner(gw, tick):
    order_id = gw.trade(TICK_ORDER).order_id
    tick.close()
    api = broker(gw, "api")
    assert api.cancel_order(TICK_ORDER) is True
    assert gw.cancels_by == [(11, order_id)]
    assert gw.trade(TICK_ORDER).status == "Cancelled"
    owner = gw.sessions[-1]
    assert (owner.client_id, owner.closed) == (11, True)
    # the tick can connect again afterwards
    broker(gw, "tick").ensure_ready()


def test_an_api_that_is_the_master_cancels_directly():
    gw = FakeIbGateway(master_client_id=16)
    broker(gw, "tick").place_order(limit_buy())
    api = broker(gw, "api", IbkrBrokerConfig(gateways=GATEWAYS, master_client_id=16))
    assert api.is_master
    assert api.cancel_order(TICK_ORDER) is True
    assert gw.cancels_by == [(16, 1)]


def test_the_apis_own_orders_are_its_to_cancel(gw, tick):
    api = broker(gw, "api")
    api.place_order(limit_buy("manual:pf_default:k1"))
    assert gw.trade("manual:pf_default:k1").client_id == 16
    # the master tick sees the API's manual order
    assert sorted(o.client_id for o in tick.open_orders()) == [TICK_ORDER, "manual:pf_default:k1"]
    assert api.cancel_order("manual:pf_default:k1") is True
    assert gw.cancels_by == [(16, 1)]


def test_the_tick_finds_the_apis_order_without_the_master_setting():
    """The owner forgot the gateway's master setting: the tick's own view
    lacks the API's manual order, so it asks every client before it would
    read the order as gone."""
    gw = FakeIbGateway(master_client_id=None)
    tick = broker(gw, "tick")
    api = broker(gw, "api")
    api.place_order(limit_buy("manual:pf_default:k1"))
    before = gw.all_open_requests
    state = tick.get_order_state("manual:pf_default:k1")
    assert state is not None and state.state not in ("cancelled", "rejected")
    assert gw.all_open_requests == before + 1
    # its own orders need no extra request
    tick.place_order(limit_buy())
    assert gw.all_open_requests == before + 1


def test_owner_sessions_open_only_other_stonks_ids(gw):
    _, gateway = pick_gateway(CONFIG)
    endpoint = endpoint_for(CONFIG, gateway, "api")
    seen = []
    open_as = owner_sessions(CONFIG, endpoint, lambda ep: seen.append(ep) or gw.session(0))
    assert open_as(0) is None  # TWS's hand-placed orders
    assert open_as(16) is None  # itself
    assert open_as(99) is None  # not ours
    assert open_as(11) is not None
    assert (seen[0].client_id, seen[0].reconnect_deadline) == (11, 0.0)


def test_read_only_roles_never_open_owner_sessions(gw):
    health = connect_ibkr(CONFIG, role="health", client_factory=lambda ep: gw.session(13))
    assert health._owner_client is None


def test_an_order_from_an_unknown_client_is_refused_with_a_hint(gw):
    gw.connect()
    gw.add_manual_order(AAPL, 5)
    # an orderRef-less order is never a ledger row, but a foreign order
    # with a ref from a client Stonks does not own is refused plainly
    stranger = gw.session(77)
    stranger.connect()
    stranger.place_order(AAPL.contract, _request("r-77"))
    api = broker(gw, "api")
    with pytest.raises(OrderOwnedElsewhereError, match="API client 77"):
        api.cancel_order("r-77")


def _request(ref: str):
    from stonks.execution.brokers.ibkr.client import IbOrderRequest

    return IbOrderRequest(action="BUY", total_quantity=1, order_type="LMT", tif="DAY",
                          order_ref=ref, account="DU1234567", limit_price=1.0)  # fmt: skip

"""A margin book's live context at IBKR (roadmap 19.13): the what-if margin
and the locate come through the adapter, and the account rules use them."""

from __future__ import annotations

from stonks.accounts.rules import AccountProfile, run_account_rules
from stonks.accounts.rules.profiles import set_profile
from stonks.core.clock import FixedClock
from stonks.core.types import Order
from stonks.execution.brokers.ibkr.broker import IbkrBroker
from stonks.execution.brokers.ibkr.client import IbShortability
from stonks.production.live.context import build_live_context
from stonks.production.rules._account_settings import AccountRulesSettings
from tests.fakes.ib_gateway import AAPL, MSFT, T0, FakeIbGateway

DAY = T0.date()
ON = AccountRulesSettings(enabled=True, margin_accounts=True, margin_buffer=0.1)
PRICES = {"AAPL.US": 200.0, "MSFT.US": 400.0}


def margin_broker() -> tuple[IbkrBroker, FakeIbGateway]:
    gw = FakeIbGateway()
    gw.margin_account(equity=100_000, initial=10_000, maintenance=5_000,
                      available_funds=90_000)  # fmt: skip
    gw.what_if_margin(AAPL, initial_per_share=100.0, maintenance_per_share=50.0)
    gw.what_if_margin(MSFT, initial_per_share=200.0, maintenance_per_share=100.0)
    gw.shortable_data[AAPL.contract.con_id] = IbShortability(AAPL.contract.con_id, 3.0, 300.0)
    gw.shortable_data[MSFT.contract.con_id] = IbShortability(MSFT.contract.con_id, 1.0, 0.0)
    broker = IbkrBroker(gw, mode="paper", account_type="margin", allow_short=True,
                        clock=FixedClock(T0))  # fmt: skip
    return broker, gw


def short(ticker: str, qty: float, i: int = 0) -> Order:
    return Order(client_id=f"s{i}", ticker=ticker, side="sell", quantity=qty,
                 position_effect="open", decision_price=PRICES[ticker])  # fmt: skip


def context(state, broker):
    set_profile(
        state,
        AccountProfile("pf_default", "us", account_type="margin", allow_short=True),
        actor="user:u",
    )
    return build_live_context(
        state, "pf_default", DAY, broker=broker, tickers=["AAPL.US", "MSFT.US"],
        account_settings=ON,
    )  # fmt: skip


def test_the_context_carries_the_what_if_and_the_locate(state):
    broker, gw = margin_broker()
    live = context(state, broker)
    inputs = live.account_rules
    assert inputs is not None and inputs.margin_preview is not None
    assert live.account is not None and live.account.reported_type == "margin"
    # the locate is asked only for tickers a rule looks up
    assert gw.shortable_requests == 0
    assert "AAPL.US" in inputs.shortable and inputs.shortable["AAPL.US"] == 300.0
    assert "MSFT.US" not in inputs.shortable  # not shortable at IBKR


def test_a_margin_book_shorts_within_the_locate_and_the_what_if(state):
    broker, gw = margin_broker()
    inputs = context(state, broker).account_rules
    assert inputs is not None
    kept, verdicts = run_account_rules(
        [short("AAPL.US", 500.0, 1), short("MSFT.US", 10.0, 2)], inputs, ON, PRICES, {}
    )
    # AAPL: IBKR lends 300 shares. MSFT: no shares to borrow.
    assert [(o.ticker, o.quantity) for o in kept] == [("AAPL.US", 300.0)]
    assert {v.rule for v in verdicts} >= {"reg_sho"}
    assert [c.con_id for c, _ in gw.what_ifs] == [AAPL.contract.con_id]


def test_the_what_if_margin_caps_a_buy(state):
    broker, gw = margin_broker()
    inputs = context(state, broker).account_rules
    assert inputs is not None
    buy = Order(client_id="b1", ticker="MSFT.US", side="buy", quantity=1_000.0,
                decision_price=400.0)  # fmt: skip
    kept, verdicts = run_account_rules([buy], inputs, ON, PRICES, {})
    # 90,000 of equity usable, 10,000 used: 80,000 / 200 per share
    # (buying power allows 90,000 / 400 = 225 shares first)
    assert kept[0].quantity == 225.0
    gw.margin_account(equity=100_000, initial=60_000, maintenance=5_000,
                      available_funds=1_000_000)  # fmt: skip
    inputs = context(state, broker).account_rules
    assert inputs is not None
    kept, verdicts = run_account_rules([buy], inputs, ON, PRICES, {})
    assert kept[0].quantity == 150.0
    assert any(v.rule == "margin_what_if" for v in verdicts)


def test_a_margin_call_at_the_broker_is_seen_by_the_rules(state):
    broker, gw = margin_broker()
    gw.margin_call(excess_liquidity=-1_000.0)
    live = context(state, broker)
    assert live.account is not None and live.account.cushion is not None
    assert live.account.cushion < 0


def test_a_cash_profile_gets_no_locate_and_no_what_if(state):
    broker, _ = margin_broker()
    set_profile(state, AccountProfile("pf_default", "us"), actor="user:u")
    live = build_live_context(state, "pf_default", DAY, broker=broker, tickers=["AAPL.US"])
    assert live.account_rules is not None
    assert live.account_rules.margin_preview is None
    assert dict(live.account_rules.shortable) == {}


def test_an_order_without_a_decision_price_is_priced_at_the_reference(state):
    broker, gw = margin_broker()
    inputs = context(state, broker).account_rules
    assert inputs is not None
    buy = Order(client_id="b1", ticker="AAPL.US", side="buy", quantity=10.0)
    kept, _ = run_account_rules([buy], inputs, ON, PRICES, {})
    assert kept == [buy]
    assert gw.what_ifs[0][1].total_quantity == 10.0

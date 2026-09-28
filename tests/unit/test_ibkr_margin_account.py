"""A margin account at IBKR (roadmap 19.13): the margin type IBKR reports,
what-if margin per share, and a margin call, all on ``FakeIbGateway``."""

from __future__ import annotations

import pytest

from stonks.core.types import Order
from stonks.execution.brokers.ibkr.broker import IbkrBroker, reported_account_type
from stonks.execution.brokers.ibkr.client import IbAccountValue
from tests.fakes.ib_gateway import AAPL, FakeIbGateway

ACCOUNT = "DU1234567"


def value(tag: str, v: str) -> IbAccountValue:
    return IbAccountValue(ACCOUNT, tag, v, "")


@pytest.mark.parametrize(
    ("tag", "raw", "expected"),
    [
        ("TradingType-S", "STKMRGN", "margin"),
        ("TradingType-S", "STKCASH", "cash"),
        ("TradingType", "PMRGN", "margin"),
        ("AccountType", "INDIVIDUAL", None),
        ("AccountType", "Reg T Margin", "margin"),
        ("AccountType", "cash", "cash"),
        ("MarginType", "REGT", "margin"),
        ("MarginType", "PORTFOLIO_MARGIN", "margin"),
    ],
)
def test_reported_account_type_reads_the_margin_type(tag, raw, expected):
    assert reported_account_type([value(tag, raw)], account_id=ACCOUNT) == expected


def test_reported_account_type_is_none_without_a_type():
    assert reported_account_type([value("NetLiquidation", "1")], account_id=ACCOUNT) is None


def test_reported_account_type_skips_other_accounts():
    rows = [IbAccountValue("DU9", "TradingType-S", "STKMRGN", "")]
    assert reported_account_type(rows, account_id=ACCOUNT) is None


def test_conflicting_types_read_as_cash():
    rows = [value("TradingType-S", "STKMRGN"), value("AccountType", "CASH")]
    assert reported_account_type(rows, account_id=ACCOUNT) == "cash"


def test_fake_margin_account_reports_margin_through_fetch_account():
    gw = FakeIbGateway()
    gw.margin_account(
        equity=20_000, excess_liquidity=6_000, maintenance=4_000, initial=5_000,
        day_trades_remaining=1,
    )  # fmt: skip
    broker = IbkrBroker(gw, mode="paper", account_type="margin", allow_short=True)
    a = broker.fetch_account()
    assert a.account_type == "margin" and a.reported_type == "margin"
    assert (a.equity, a.excess_liquidity, a.maintenance_margin, a.initial_margin) == (
        20_000, 6_000, 4_000, 5_000,
    )  # fmt: skip
    assert a.day_trades_remaining == 1


def test_a_cash_account_reports_cash():
    gw = FakeIbGateway()
    gw.set_values(NetLiquidation="1000", **{"TradingType-S": "STKCASH"})
    a = IbkrBroker(gw, mode="paper").fetch_account()
    assert a.reported_type == "cash"


def test_what_if_margin_scales_with_the_quantity():
    gw = FakeIbGateway()
    gw.margin_account(equity=50_000)
    gw.what_if_margin(AAPL, initial_per_share=100.0, maintenance_per_share=50.0)
    broker = IbkrBroker(gw, mode="paper", account_type="margin")
    order = Order(client_id="c1", ticker="AAPL.US", side="buy", quantity=10.0,
                  decision_price=200.0)  # fmt: skip
    p = broker.what_if(order)
    assert (p.initial_margin_change, p.maintenance_margin_change) == (1000.0, 500.0)
    assert p.equity_with_loan_after == 50_000


def test_margin_call_drops_excess_liquidity_below_zero():
    gw = FakeIbGateway()
    gw.margin_account(equity=20_000, excess_liquidity=3_000, maintenance=17_000)
    gw.margin_call(excess_liquidity=-500.0)
    a = IbkrBroker(gw, mode="paper", account_type="margin").fetch_account()
    assert a.excess_liquidity == -500.0
    assert a.maintenance_margin == 20_500.0

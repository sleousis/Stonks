"""IBKR Flex statements as vendor-free broker statements (roadmap 19.15),
and the shared statement cache. No network."""

from __future__ import annotations

from datetime import date

import pytest

from stonks.execution.brokers.ibkr import statements as st
from stonks.execution.brokers.ibkr.flex import FlexClient, FlexError, parse_flex_statements
from stonks.execution.brokers.ibkr.settings import IbkrFlexSettings
from stonks.execution.drift import StatementCash, StatementExecution
from tests.unit.test_ibkr_flex import SEND_OK, STATEMENT, TOKEN, Transport


@pytest.fixture(autouse=True)
def _fresh_cache():
    st.clear_statement_cache()
    yield
    st.clear_statement_cache()


def test_a_flex_statement_maps_to_a_broker_statement():
    (flex,) = parse_flex_statements(STATEMENT)
    out = st.to_broker_statement(flex)
    assert (out.account_id, out.from_date, out.to_date) == (
        "U1234567",
        date(2026, 9, 21),
        date(2026, 9, 25),
    )
    buy, sell = out.executions
    assert buy == StatementExecution(
        exec_id="0001f4e8.1",
        quantity=10.0,
        symbol="AAPL",
        order_ref="t1-s1-AAPL.US-buy",
        trade_date=date(2026, 9, 25),
        settle_date=date(2026, 9, 29),
        cash=-2005.0,
        commission=1.0,
        commission_currency="USD",
        currency="USD",
    )
    assert (sell.quantity, sell.cash, sell.commission, sell.order_ref) == (-3.0, 1350.0, 0.5, None)
    dividend, deposit = out.cash
    assert dividend == StatementCash(
        kind="dividend",
        amount=2.5,
        date=date(2026, 9, 15),
        settle_date=date(2026, 9, 15),
        currency="USD",
        symbol="AAPL",
    )
    assert (deposit.kind, deposit.amount, deposit.symbol) == ("other", 5000.0, None)


def test_a_trade_without_proceeds_uses_quantity_times_price():
    text = STATEMENT.replace('proceeds="-2005" ', "")
    (flex,) = parse_flex_statements(text)
    buy = st.to_broker_statement(flex).executions[0]
    assert buy.cash == pytest.approx(-2005.0)


def test_a_trade_with_no_execution_id_is_left_out():
    text = STATEMENT.replace('ibExecID="0001f4e8.2" ', "")
    (flex,) = parse_flex_statements(text)
    assert [e.exec_id for e in st.to_broker_statement(flex).executions] == ["0001f4e8.1"]


def flex_client(transport: Transport) -> FlexClient:
    settings = IbkrFlexSettings(query_id="555", poll_seconds=0.0, max_polls=3)
    return FlexClient(TOKEN, settings, transport=transport, sleep=lambda s: None)


def test_statements_are_cached_per_query():
    transport = Transport(SEND_OK, STATEMENT)
    client = flex_client(transport)
    first = st.cached_statements(client)
    second = st.cached_statements(client)
    assert first is second and len(transport.calls) == 2


def test_the_statement_source_returns_broker_statements_and_raises_flex_errors():
    source = st.statement_source(flex_client(Transport(SEND_OK, STATEMENT)))
    [statement] = source()
    assert statement.account_id == "U1234567"
    broken = st.statement_source(flex_client(Transport(RuntimeError("boom"))))
    st.clear_statement_cache()
    with pytest.raises(FlexError):
        broken()


def test_a_corrected_execution_keeps_the_id_the_ledger_booked():
    """A correction's id differs only after the last period. The statement
    names it by the first version's id, the one reconciliation books."""
    text = STATEMENT.replace('ibExecID="0001f4e8.1"', 'ibExecID="0000e0d5.6576f7c6.01.02"')
    assert text != STATEMENT
    (flex,) = parse_flex_statements(text)
    buy, _ = st.to_broker_statement(flex).executions
    assert buy.exec_id == "0000e0d5.6576f7c6.01.01"

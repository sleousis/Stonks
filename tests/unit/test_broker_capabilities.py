"""The optional live-broker capabilities (roadmap 19.1).

Each capability is a ``runtime_checkable`` protocol next to
``OrderStateSource`` and ``OrderCanceller``: code that needs one checks
``isinstance`` and degrades when a broker lacks it, so the simulated broker
keeps working unchanged.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from stonks.backtest.simulated_broker import SimulatedBroker
from stonks.core.types import Order, Portfolio
from stonks.execution.brokers.base import (
    AccountReader,
    BrokerError,
    BrokerUnavailableError,
    Execution,
    ExecutionSource,
    GlobalCanceller,
    LiveAccountState,
    MarginPreview,
    MarginPreviewer,
    Quote,
    QuoteSource,
    broker_capabilities,
)

NOW = datetime(2026, 9, 28, 14, 30, tzinfo=UTC)


class _LiveBroker:
    """Implements every optional capability (a stand-in for IbkrBroker)."""

    def fetch_portfolio(self) -> Portfolio:
        return Portfolio(cash=0.0)

    def place_order(self, order: Order):  # pragma: no cover - not called
        raise NotImplementedError

    def reconcile(self):
        return []

    def cancel_all(self) -> int:
        return 0

    def fetch_account(self) -> LiveAccountState:
        return LiveAccountState(
            equity=1000.0,
            cash=1000.0,
            settled_cash=900.0,
            available_funds=900.0,
            buying_power=900.0,
            currency="USD",
            account_type="cash",
        )

    def what_if(self, order: Order) -> MarginPreview:
        return MarginPreview(
            client_id=order.client_id,
            initial_margin_change=0.0,
            maintenance_margin_change=0.0,
            equity_with_loan_after=1000.0,
            commission=1.0,
            commission_currency="USD",
        )

    def executions(self, since: datetime) -> list[Execution]:
        return []

    def quotes(self, tickers):
        return {}


def test_a_live_broker_has_every_capability():
    broker = _LiveBroker()
    assert isinstance(broker, GlobalCanceller)
    assert isinstance(broker, AccountReader)
    assert isinstance(broker, MarginPreviewer)
    assert isinstance(broker, ExecutionSource)
    assert isinstance(broker, QuoteSource)
    assert broker_capabilities(broker) >= {
        "global_cancel",
        "account",
        "what_if",
        "executions",
        "quotes",
    }


def test_the_simulated_broker_has_none_of_the_live_capabilities():
    broker = SimulatedBroker(Portfolio(cash=1000.0))
    caps = broker_capabilities(broker)
    assert not caps & {"global_cancel", "account", "what_if", "executions", "quotes"}


def test_broker_unavailable_is_a_broker_error():
    assert issubclass(BrokerUnavailableError, BrokerError)


def test_live_account_state_reads_cash_per_currency():
    state = LiveAccountState(
        equity=10_000.0,
        cash=5_000.0,
        settled_cash=4_000.0,
        available_funds=4_000.0,
        buying_power=4_000.0,
        currency="EUR",
        account_type="cash",
        cash_by_currency={"EUR": 3_000.0, "USD": 2_200.0},
    )
    assert state.cash_in("USD") == 2_200.0
    assert state.cash_in("GBP") == 0.0
    # the base currency falls back to cash when the ledger is empty
    bare = LiveAccountState(
        equity=1.0,
        cash=1.0,
        settled_cash=1.0,
        available_funds=1.0,
        buying_power=1.0,
        currency="USD",
        account_type="margin",
    )
    assert bare.cash_in("USD") == 1.0


def test_quote_mid_and_liveness():
    live = Quote(ticker="AAPL.US", last=100.0, bid=99.9, ask=100.1, as_of=NOW, delayed=False)
    assert live.mid == pytest.approx(100.0)
    assert live.reference == 100.0
    no_last = Quote(ticker="AAPL.US", last=None, bid=99.0, ask=101.0, as_of=NOW, delayed=True)
    assert no_last.reference == pytest.approx(100.0)
    empty = Quote(ticker="AAPL.US", last=None, bid=None, ask=None, as_of=NOW, delayed=True)
    assert empty.mid is None and empty.reference is None


def test_execution_signed_quantity_and_fill():
    execution = Execution(
        broker_exec_id="0001.01",
        client_id="c1",
        ticker="AAPL.US",
        side="sell",
        quantity=3.0,
        price=10.0,
        executed_at=NOW,
        commission=1.25,
        commission_currency="USD",
    )
    fill = execution.to_fill()
    assert fill.broker_exec_id == "0001.01"
    assert fill.fee == 1.25
    assert fill.fee_currency == "USD"
    assert fill.side == "sell" and fill.quantity == 3.0
    assert (
        Execution(
            broker_exec_id="x",
            client_id="c",
            ticker="T",
            side="buy",
            quantity=1.0,
            price=1.0,
            executed_at=NOW,
        )
        .to_fill()
        .fee
        == 0.0
    )

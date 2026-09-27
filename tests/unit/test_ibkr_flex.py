"""IBKR Flex statements (roadmap 19.3): the two-step Web Service fetch over an
injected transport, retries while the statement is generated, error codes,
token hygiene and the statement parser. No network."""

from __future__ import annotations

from datetime import date

import pytest

from stonks.execution.brokers.ibkr.flex import (
    FlexAuthError,
    FlexClient,
    FlexError,
    parse_flex_statements,
)
from stonks.execution.brokers.ibkr.settings import IbkrBrokerConfig, IbkrFlexSettings

TOKEN = "123456789012345678901234"

SEND_OK = """<FlexStatementResponse timestamp="28 September, 2026 08:30 AM EDT">
<Status>Success</Status><ReferenceCode>REF42</ReferenceCode>
<Url>https://flex.example/GetStatement</Url></FlexStatementResponse>"""

NOT_READY = """<FlexStatementResponse><Status>Warn</Status><ErrorCode>1019</ErrorCode>
<ErrorMessage>Statement generation in progress. Please try again shortly.</ErrorMessage>
</FlexStatementResponse>"""

BAD_TOKEN = """<FlexStatementResponse><Status>Fail</Status><ErrorCode>1015</ErrorCode>
<ErrorMessage>Token is invalid.</ErrorMessage></FlexStatementResponse>"""

STATEMENT = """<FlexQueryResponse queryName="stonks" type="AF">
<FlexStatements count="1">
<FlexStatement accountId="U1234567" fromDate="20260921" toDate="20260925"
  whenGenerated="20260928;083000">
<Trades>
<Trade accountId="U1234567" currency="USD" assetCategory="STK" symbol="AAPL" conid="265598"
  isin="US0378331005" listingExchange="NASDAQ" tradeID="9001" ibExecID="0001f4e8.1"
  tradeDate="20260925" settleDateTarget="20260929" quantity="10" tradePrice="200.5"
  proceeds="-2005" ibCommission="-1.0" ibCommissionCurrency="USD" buySell="BUY"
  orderReference="t1-s1-AAPL.US-buy" description="APPLE INC" levelOfDetail="EXECUTION"/>
<Trade accountId="U1234567" currency="USD" assetCategory="STK" symbol="AAPL" conid="265598"
  tradeID="9001" tradeDate="20260925" quantity="10" tradePrice="200.5" buySell="BUY"
  levelOfDetail="ORDER"/>
<Trade accountId="U1234567" currency="USD" assetCategory="STK" symbol="BRK B" conid="72063691"
  tradeID="9002" ibExecID="0001f4e8.2" tradeDate="2026-09-24" quantity="-3" tradePrice="450"
  proceeds="1350" ibCommission="-0.5" ibCommissionCurrency="USD" buySell="SELL"
  orderReference="" description="BERKSHIRE" levelOfDetail="EXECUTION" listingExchange="NYSE"/>
</Trades>
<CashTransactions>
<CashTransaction accountId="U1234567" currency="USD" type="Dividends" amount="2.5"
  dateTime="20260915;202000" settleDate="20260915" transactionID="7001" symbol="AAPL"
  conid="265598" description="AAPL CASH DIVIDEND"/>
<CashTransaction accountId="U1234567" currency="USD" type="Deposits/Withdrawals"
  amount="5000" dateTime="2026-09-01" transactionID="7002" symbol="" description="DEPOSIT"/>
</CashTransactions>
</FlexStatement>
</FlexStatements>
</FlexQueryResponse>"""


class Transport:
    def __init__(self, *answers: str) -> None:
        self.answers = list(answers)
        self.calls: list[tuple[str, dict[str, str]]] = []

    def __call__(self, url: str, params: dict[str, str]) -> str:
        self.calls.append((url, dict(params)))
        answer = self.answers.pop(0)
        if isinstance(answer, BaseException):
            raise answer
        return answer


def client(transport: Transport, **kw) -> FlexClient:
    settings = IbkrFlexSettings(query_id="555", poll_seconds=0.0, max_polls=3)
    return FlexClient(TOKEN, settings, transport=transport, sleep=lambda s: None, **kw)


def test_parse_keeps_execution_rows_and_cash_transactions():
    (st,) = parse_flex_statements(STATEMENT)
    assert (st.account_id, st.from_date, st.to_date) == ("U1234567", date(2026, 9, 21),
                                                         date(2026, 9, 25))  # fmt: skip
    assert [t.exec_id for t in st.trades] == ["0001f4e8.1", "0001f4e8.2"]
    aapl, brk = st.trades
    assert (aapl.symbol, aapl.quantity, aapl.price, aapl.commission) == ("AAPL", 10, 200.5, -1.0)
    assert (aapl.trade_date, aapl.settle_date) == (date(2026, 9, 25), date(2026, 9, 29))
    assert (aapl.order_ref, aapl.isin, aapl.con_id, aapl.currency) == (
        "t1-s1-AAPL.US-buy", "US0378331005", 265598, "USD",
    )  # fmt: skip
    assert (brk.quantity, brk.order_ref, brk.trade_date) == (-3, None, date(2026, 9, 24))
    div, dep = st.cash_transactions
    assert (div.transaction_id, div.type, div.amount, div.date) == (
        "7001", "Dividends", 2.5, date(2026, 9, 15),
    )  # fmt: skip
    assert (dep.type, dep.amount, dep.symbol) == ("Deposits/Withdrawals", 5000.0, None)


def test_parse_refuses_something_that_is_not_a_statement():
    with pytest.raises(FlexError):
        parse_flex_statements("<html>nope</html>")
    with pytest.raises(FlexError):
        parse_flex_statements("not xml at all <")


def test_fetch_sends_then_polls_until_the_statement_is_ready():
    t = Transport(SEND_OK, NOT_READY, STATEMENT)
    (st,) = client(t).fetch()
    assert st.account_id == "U1234567"
    send, poll1, poll2 = t.calls
    assert send[0].endswith("/SendRequest")
    assert send[1] == {"t": TOKEN, "q": "555", "v": "3"}
    assert poll1 == ("https://flex.example/GetStatement", {"t": TOKEN, "q": "REF42", "v": "3"})
    assert poll2 == poll1


def test_fetch_gives_up_after_max_polls():
    t = Transport(SEND_OK, NOT_READY, NOT_READY, NOT_READY)
    with pytest.raises(FlexError, match="not ready"):
        client(t).fetch()


def test_a_refused_token_is_an_auth_error_without_the_token():
    t = Transport(BAD_TOKEN)
    with pytest.raises(FlexAuthError) as err:
        client(t).fetch()
    assert "1015" in str(err.value)
    assert TOKEN not in str(err.value)


def test_transport_errors_never_carry_the_token():
    t = Transport(OSError(f"GET https://x/?t={TOKEN} failed"))
    with pytest.raises(FlexError) as err:
        client(t).fetch()
    assert TOKEN not in str(err.value)


def test_client_needs_a_token_and_a_query():
    with pytest.raises(FlexError, match="STONKS_IBKR_FLEX_TOKEN"):
        FlexClient("", IbkrFlexSettings(query_id="1"))
    with pytest.raises(FlexError, match="query_id"):
        FlexClient(TOKEN, IbkrFlexSettings())
    assert repr(TOKEN) not in repr(FlexClient(TOKEN, IbkrFlexSettings(query_id="1")))


def test_flex_settings_refuse_a_token_in_toml():
    with pytest.raises(ValueError, match="STONKS_IBKR_FLEX_TOKEN"):
        IbkrBrokerConfig(flex={"query_id": "1", "token": "abc"})
    assert IbkrBrokerConfig(flex={"query_id": "1"}).flex.query_id == "1"


def test_from_env_reads_the_token(monkeypatch):
    settings = IbkrFlexSettings(query_id="9")
    monkeypatch.delenv("STONKS_IBKR_FLEX_TOKEN", raising=False)
    assert FlexClient.from_env(settings) is None
    monkeypatch.setenv("STONKS_IBKR_FLEX_TOKEN", TOKEN)
    assert FlexClient.from_env(IbkrFlexSettings()) is None  # no query configured
    assert isinstance(FlexClient.from_env(settings), FlexClient)

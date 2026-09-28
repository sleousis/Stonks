"""The checks shown before a kill switch resume (roadmap 23.15): the
gateway is up, the last reconcile was clean, the account reads, and its
equity covers a multiple of the largest position."""

from __future__ import annotations

from datetime import UTC, datetime

from stonks.core.types import Portfolio
from stonks.execution.brokers.base import BrokerUnavailableError, LiveAccountState, Quote
from stonks.execution.brokers.ibkr.broker import IbkrBroker
from stonks.execution.brokers.ibkr.client import IbSnapshot
from stonks.production.live.settings import ResumeCheckSettings
from stonks.production.resume_checks import checks_passed, portfolio_resume_checks
from tests.fakes.ib_gateway import AAPL, FakeIbGateway

PF = "pf_live"
NOW = datetime(2026, 9, 25, 20, 30, tzinfo=UTC)


def account(equity: float) -> LiveAccountState:
    return LiveAccountState(
        equity=equity,
        cash=equity,
        settled_cash=equity,
        available_funds=equity,
        buying_power=equity,
        currency="USD",
        account_type="cash",
    )


class StubBroker:
    def __init__(self, equity=100_000.0, positions=None, quotes=None, down=False):
        self.equity = equity
        self.positions = positions or {}
        self.quote_map = quotes or {}
        self.down = down

    def fetch_portfolio(self) -> Portfolio:
        if self.down:
            raise BrokerUnavailableError("gateway down")
        return Portfolio(cash=self.equity, positions=dict(self.positions))

    def fetch_account(self) -> LiveAccountState:
        if self.down:
            raise BrokerUnavailableError("gateway down")
        return account(self.equity)

    def quotes(self, tickers):
        return {t: self.quote_map[t] for t in tickers if t in self.quote_map}


def report(state, status: str, taken_at: str = "2026-09-25T20:15:00+00:00") -> None:
    state.execute(
        "INSERT INTO reconcile_reports (id, portfolio_id, kind, as_of, taken_at, status)"
        " VALUES (?, ?, 'eod', ?, ?, ?)",
        [f"rec_{taken_at}", PF, taken_at[:10], taken_at, status],
    )


def by_name(checks):
    return {c.name: c for c in checks}


def test_a_book_at_no_broker_has_nothing_to_check(state):
    checks = portfolio_resume_checks(state, PF, None, settings=ResumeCheckSettings())
    assert [(c.name, c.passed) for c in checks] == [("broker", None)]
    assert checks_passed(checks)


def test_every_check_passes_on_a_healthy_account(state):
    report(state, "clean")
    broker = StubBroker(equity=100_000.0, positions={"AAPL.US": 100.0},
                        quotes={"AAPL.US": Quote(ticker="AAPL.US", last=200.0, bid=None, ask=None, as_of=NOW, delayed=True)})  # fmt: skip
    checks = by_name(portfolio_resume_checks(state, PF, broker, settings=ResumeCheckSettings()))
    assert {n: c.passed for n, c in checks.items()} == {
        "gateway_up": True,
        "last_reconcile_clean": True,
        "account_readable": True,
        "equity_cover": True,
    }
    assert "5.0x" in checks["equity_cover"].detail


def test_a_down_gateway_fails_and_nothing_else_is_guessed(state):
    report(state, "clean")
    checks = by_name(
        portfolio_resume_checks(state, PF, StubBroker(down=True), settings=ResumeCheckSettings())
    )
    assert checks["gateway_up"].passed is False and "gateway down" in checks["gateway_up"].detail
    assert checks["account_readable"].passed is False
    assert checks["equity_cover"].passed is None
    assert not checks_passed(list(checks.values()))


def test_the_last_reconcile_decides(state):
    broker = StubBroker()
    first = by_name(portfolio_resume_checks(state, PF, broker, settings=ResumeCheckSettings()))
    assert first["last_reconcile_clean"].passed is None  # no report yet: shown, not blocking
    report(state, "clean", "2026-09-24T20:15:00+00:00")
    report(state, "drift", "2026-09-25T20:15:00+00:00")
    later = by_name(portfolio_resume_checks(state, PF, broker, settings=ResumeCheckSettings()))
    assert later["last_reconcile_clean"].passed is False
    assert "drift" in later["last_reconcile_clean"].detail


def test_equity_must_cover_a_multiple_of_the_largest_position(state):
    broker = StubBroker(equity=10_000.0, positions={"AAPL.US": 50.0, "MSFT.US": -10.0})
    prices = {"AAPL.US": 180.0, "MSFT.US": 400.0}
    checks = by_name(
        portfolio_resume_checks(
            state, PF, broker, settings=ResumeCheckSettings(min_equity_multiple=1.5), prices=prices
        )
    )
    # the largest is AAPL at 9,000: 1.1x, under 1.5x
    assert checks["equity_cover"].passed is False
    assert "AAPL.US" in checks["equity_cover"].detail
    flat = by_name(portfolio_resume_checks(state, PF, StubBroker(), settings=ResumeCheckSettings()))
    assert flat["equity_cover"].passed is True and "no positions" in flat["equity_cover"].detail
    unpriced = by_name(
        portfolio_resume_checks(
            state, PF, StubBroker(positions={"ZZZ.US": 5.0}), settings=ResumeCheckSettings()
        )
    )
    assert unpriced["equity_cover"].passed is None


def test_against_the_fake_ib_gateway(state):
    gw = FakeIbGateway()
    gw.set_values(NetLiquidation="100000", TotalCashValue="50000")
    gw.set_position(AAPL, 10.0)
    gw.snapshot_data[AAPL.contract.con_id] = IbSnapshot(
        con_id=AAPL.contract.con_id, last=200.0, bid=None, ask=None, time=gw.now
    )
    report(state, "clean")
    broker = IbkrBroker(gw, mode="paper")
    checks = by_name(portfolio_resume_checks(state, PF, broker, settings=ResumeCheckSettings()))
    assert all(c.passed for c in checks.values()), checks
    gw.drop()
    gw.connect_failures = 5
    down = by_name(
        portfolio_resume_checks(
            state, PF, IbkrBroker(gw, mode="paper"), settings=ResumeCheckSettings()
        )
    )
    assert down["gateway_up"].passed is False

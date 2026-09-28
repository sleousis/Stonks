"""The account rules' state (roadmap 19.7): profiles with an audit row,
settlement dates per market, day trades and loss sales from the book's own
fills, and the ``account_rules`` risk rule on a live book."""

from __future__ import annotations

import json
from datetime import date

import pytest

from stonks.accounts.rules import AccountProfile
from stonks.accounts.rules.inputs import day_trades, load_account_inputs, loss_sales
from stonks.accounts.rules.profiles import ProfileError, get_profile, set_profile
from stonks.accounts.rules.settlement import load_settlements, record_settlements, settle_date
from stonks.core.types import Order, Portfolio
from stonks.execution.brokers.base import LiveAccountState
from stonks.production.live.context import LiveContext
from stonks.production.rules import registered_rules
from stonks.production.rules._account_settings import AccountRulesSettings
from tests.fixtures.risk_rules import context, policy

SETTINGS = AccountRulesSettings(enabled=True)
FRIDAY = date(2026, 9, 25)


def test_settlement_follows_the_market_and_skips_weekends():
    assert settle_date(FRIDAY, "US", SETTINGS) == date(2026, 9, 28)
    assert settle_date(FRIDAY, "LSE", SETTINGS) == date(2026, 9, 29)
    assert settle_date(FRIDAY, "XX", SETTINGS) == date(2026, 9, 29)  # the default T+2
    t1 = AccountRulesSettings(settlement_days={"LSE": 1})
    assert settle_date(FRIDAY, "LSE", t1) == date(2026, 9, 28)


def test_profile_round_trip_is_audited(state):
    assert get_profile(state, "pf_default") is None
    prof = AccountProfile(portfolio_id="pf_default", jurisdiction="uk", base_currency="gbp")
    got = set_profile(state, prof, actor="user:usr_owner")
    assert got.account_type == "cash" and got.base_currency == "GBP"
    row = state.sql("SELECT details_json FROM audit_log WHERE action = 'live.account_profile_set'")
    assert json.loads(row[0]["details_json"])["previous"] is None


def _tax_row(state):
    rows = state.sql(
        "SELECT jurisdiction, wash_sales FROM portfolio_tax_settings WHERE portfolio_id = ?",
        ["pf_default"],
    )
    return rows[0] if rows else None


def _base(state) -> str:
    return state.sql("SELECT base_currency FROM portfolios WHERE id = 'pf_default'")[0][0]


def test_jurisdiction_and_base_currency_have_one_home(state):
    """The tax settings hold the jurisdiction and portfolios the base
    currency. The account profile reads and writes them there."""
    assert "jurisdiction" not in _cols(state, "account_profiles")
    assert "base_currency" not in _cols(state, "account_profiles")
    set_profile(
        state,
        AccountProfile(portfolio_id="pf_default", jurisdiction="uk", base_currency="gbp"),
        actor="user:u",
    )
    assert _tax_row(state)["jurisdiction"] == "uk" and _base(state) == "GBP"
    # a change on the tax side is what the account rules see
    state.execute(
        "UPDATE portfolio_tax_settings SET jurisdiction = 'eu' WHERE portfolio_id = 'pf_default'"
    )
    state.execute("UPDATE portfolios SET base_currency = 'EUR' WHERE id = 'pf_default'")
    got = get_profile(state, "pf_default")
    assert got is not None and (got.jurisdiction, got.base_currency) == ("eu", "EUR")


def test_the_wash_sale_guard_follows_the_tax_setting(state):
    set_profile(state, AccountProfile("pf_default", "us", wash_sale_mode="block"), actor="user:u")
    got = get_profile(state, "pf_default")
    assert got is not None and got.wash_sales
    state.execute(
        "UPDATE portfolio_tax_settings SET wash_sales = 0 WHERE portfolio_id = 'pf_default'"
    )
    got = get_profile(state, "pf_default")
    assert got is not None and not got.wash_sales


def _cols(state, table: str) -> set[str]:
    return {r["name"] for r in state.sql(f"PRAGMA table_info({table})")}


def test_a_cash_profile_cannot_allow_shorts(state):
    with pytest.raises(ProfileError):
        set_profile(
            state,
            AccountProfile(portfolio_id="pf_default", jurisdiction="us", allow_short=True),
            actor="user:u",
        )


def _fill(state, cid, side, qty, price, day):
    state.execute(
        "INSERT INTO orders (client_id, ticker, side, quantity, order_type, status, created_at,"
        " updated_at, portfolio_id) VALUES (?, 'AAPL.US', ?, ?, 'market', 'filled', 'x', 'x',"
        " 'pf_default')",
        [cid, side, qty],
    )
    state.execute(
        "INSERT INTO fills (order_client_id, ticker, quantity, price, fee, filled_at,"
        " portfolio_id) VALUES (?, 'AAPL.US', ?, ?, 1.0, ?, 'pf_default')",
        [cid, qty, price, f"{day.isoformat()}T14:30:00"],
    )


def test_settlements_are_read_before_and_after_they_are_recorded(state):
    _fill(state, "c1", "buy", 10.0, 100.0, FRIDAY)
    _fill(state, "c2", "sell", 10.0, 90.0, date(2026, 9, 28))
    usd = lambda _t: "USD"  # noqa: E731
    before = load_settlements(
        state, "pf_default", SETTINGS, currency_of=usd, as_of=date(2026, 9, 28)
    )
    assert [(e.side, e.amount, e.settle_date) for e in before] == [
        ("buy", -1001.0, date(2026, 9, 28)),
        ("sell", 899.0, date(2026, 9, 29)),
    ]
    assert (
        record_settlements(state, "pf_default", SETTINGS, currency_of=usd, as_of=date(2026, 9, 28))
        == 2
    )
    assert (
        record_settlements(state, "pf_default", SETTINGS, currency_of=usd, as_of=date(2026, 9, 28))
        == 0
    )
    after = load_settlements(
        state, "pf_default", SETTINGS, currency_of=usd, as_of=date(2026, 9, 28)
    )
    assert after == before


def test_a_commission_in_another_currency_is_converted_before_it_enters_the_ledger(state):
    from stonks.fx import FxRates

    _fill(state, "c1", "buy", 10.0, 100.0, FRIDAY)
    _fill(state, "c2", "sell", 10.0, 90.0, date(2026, 9, 28))
    state.execute("UPDATE fills SET fee = 2.0, fee_currency = 'EUR'")
    usd = lambda _t: "USD"  # noqa: E731
    fx = FxRates([("EUR", "USD", date(2026, 9, 1), 1.5)])
    got = load_settlements(
        state, "pf_default", SETTINGS, currency_of=usd, as_of=date(2026, 9, 28), fx=fx
    )
    assert [(e.currency, e.amount) for e in got] == [("USD", -1003.0), ("USD", 897.0)]
    assert (
        record_settlements(
            state, "pf_default", SETTINGS, currency_of=usd, as_of=date(2026, 9, 28), fx=fx
        )
        == 2
    )
    stored = state.sql("SELECT amount FROM settlement_ledger ORDER BY id")
    assert [r["amount"] for r in stored] == [-1003.0, 897.0]


def test_a_commission_with_no_rate_is_never_guessed_or_recorded(state):
    _fill(state, "c2", "sell", 10.0, 90.0, date(2026, 9, 28))
    state.execute("UPDATE fills SET fee = 2.0, fee_currency = 'EUR'")
    usd = lambda _t: "USD"  # noqa: E731
    got = load_settlements(state, "pf_default", SETTINGS, currency_of=usd, as_of=date(2026, 9, 28))
    # Unsettled proceeds count in full (the stricter figure) until a rate exists.
    assert [e.amount for e in got] == [900.0]
    assert (
        record_settlements(state, "pf_default", SETTINGS, currency_of=usd, as_of=date(2026, 9, 28))
        == 0
    )


def test_day_trades_and_loss_sales_from_fills():
    fills = [
        ("AAPL.US", "buy", 10.0, 100.0, date(2026, 9, 22)),
        ("AAPL.US", "sell", 10.0, 95.0, date(2026, 9, 22)),
        ("MSFT.US", "buy", 1.0, 300.0, date(2026, 9, 1)),
        ("MSFT.US", "sell", 1.0, 310.0, date(2026, 9, 24)),
    ]
    assert day_trades(fills, date(2026, 9, 28), 5) == [date(2026, 9, 22)]
    assert day_trades(fills, date(2026, 9, 30), 5) == []  # out of the window
    assert loss_sales(fills, date(2026, 9, 28), 30) == {"AAPL.US": date(2026, 9, 22)}


def test_load_inputs_needs_a_profile(state):
    assert load_account_inputs(state, "pf_default", FRIDAY, SETTINGS) is None
    set_profile(state, AccountProfile("pf_default", "us"), actor="user:u")
    state.execute(
        "INSERT INTO account_restricted (portfolio_id, ticker, reason, source, created_at)"
        " VALUES ('pf_default', 'GME.US', 'owner list', 'owner', 'x')"
    )
    _fill(state, "c1", "buy", 1.0, 10.0, FRIDAY)
    inputs = load_account_inputs(state, "pf_default", FRIDAY, SETTINGS)
    assert inputs is not None
    assert inputs.restricted == {"GME.US": "owner list"}
    assert inputs.opened_today == frozenset({"AAPL.US"})
    assert len(inputs.settlements) == 1


def _account() -> LiveAccountState:
    return LiveAccountState(
        equity=1_000.0,
        cash=1_000.0,
        settled_cash=300.0,
        available_funds=300.0,
        buying_power=300.0,
        currency="USD",
        account_type="cash",
    )


def test_the_account_rules_risk_rule_on_a_live_book(state):
    rule = next(r for r in registered_rules() if r.name == "account_rules")
    pol = policy(account_rules={"enabled": True})
    assert rule.enabled(pol) and not rule.enabled(policy())
    orders = [Order(client_id="b", ticker="AAPL.US", side="buy", quantity=5.0)]
    prices = {"AAPL.US": 100.0}
    # a paper book: untouched
    paper = context(Portfolio(cash=1e6), prices, pol)
    assert rule.apply(orders, paper) == (orders, [])
    # a live book without a profile opens nothing
    bare = context(Portfolio(cash=1e6), prices, pol, live=LiveContext(portfolio_id="pf_default"))
    kept, adj = rule.apply(orders, bare)
    assert kept == [] and adj[0].rule == "account_rules.profile"
    # with a profile: a cash account spends settled cash only
    set_profile(state, AccountProfile("pf_default", "us"), actor="user:u")
    inputs = load_account_inputs(state, "pf_default", FRIDAY, SETTINGS, account=_account())
    live = LiveContext(portfolio_id="pf_default", account=_account(), account_rules=inputs)
    kept, adj = rule.apply(orders, context(Portfolio(cash=1e6), prices, pol, live=live))
    assert kept[0].quantity == pytest.approx(3.0)
    assert adj[0].rule == "account_rules.settled_cash"

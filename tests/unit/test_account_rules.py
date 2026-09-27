"""The account rules engine (roadmap 19.7): rules per jurisdiction and
account type, settled cash for a cash account, and closes never dropped."""

from __future__ import annotations

from dataclasses import replace
from datetime import date

import pytest

from stonks.accounts.rules import (
    AccountProfile,
    AccountRuleInputs,
    InstrumentFacts,
    SettlementEntry,
    registered_account_rules,
    run_account_rules,
)
from stonks.core.types import Order
from stonks.execution.brokers.base import LiveAccountState
from stonks.production.rules._account_settings import AccountRulesSettings

AS_OF = date(2026, 9, 28)  # a Monday
SETTINGS = AccountRulesSettings(enabled=True)
PRICES = {"AAPL.US": 100.0, "SPY.US": 500.0, "VUSA.LSE": 80.0, "SAP.XETRA": 200.0}


def account(**kw) -> LiveAccountState:
    base: dict[str, object] = {
        "equity": 10_000.0,
        "cash": 10_000.0,
        "settled_cash": 10_000.0,
        "available_funds": 10_000.0,
        "buying_power": 10_000.0,
        "currency": "USD",
        "account_type": "cash",
    }
    base.update(kw)
    return LiveAccountState(**base)  # type: ignore[arg-type]


def profile(jurisdiction="us", **kw) -> AccountProfile:
    return AccountProfile(portfolio_id="pf_live", jurisdiction=jurisdiction, **kw)


def inputs(prof=None, **kw) -> AccountRuleInputs:
    kw.setdefault("account", account())
    return AccountRuleInputs(profile=prof or profile(), as_of=AS_OF, **kw)


def buy(ticker, qty, i=0) -> Order:
    return Order(client_id=f"b{i}", ticker=ticker, side="buy", quantity=qty)


def sell(ticker, qty, i=0, effect=None) -> Order:
    return Order(
        client_id=f"s{i}", ticker=ticker, side="sell", quantity=qty, position_effect=effect
    )


def run(orders, inp, positions=None, prices=PRICES):
    return run_account_rules(orders, inp, SETTINGS, prices, positions or {})


def rules_named(verdicts):
    return {v.rule for v in verdicts}


def test_rules_are_registered_and_filtered_per_profile():
    names = {r.name for r in registered_account_rules()}
    assert {
        "restricted",
        "short_permission",
        "settled_cash",
        "buying_power",
        "fx_funding",
        "pdt",
        "wash_sale",
        "reg_sho",
        "priips_kid",
        "short_disclosure",
    } <= names
    us_cash = {r.name for r in registered_account_rules(profile("us"))}
    assert "settled_cash" in us_cash and "pdt" not in us_cash and "priips_kid" not in us_cash
    uk_margin = {r.name for r in registered_account_rules(profile("uk", account_type="margin"))}
    assert {"priips_kid", "buying_power"} <= uk_margin and "wash_sale" not in uk_margin


# ---- cash account: settled cash, no free-riding, no shorts, no margin -------------


def test_cash_account_buys_only_with_settled_cash_net_of_the_run():
    inp = inputs(account=account(settled_cash=1_500.0, cash=1_500.0))
    kept, verdicts = run([buy("AAPL.US", 10.0, 1), buy("AAPL.US", 10.0, 2)], inp)
    assert [o.quantity for o in kept] == [10.0, 5.0]
    assert rules_named(verdicts) == {"settled_cash"}


def test_sale_proceeds_do_not_fund_a_buy_in_the_same_run():
    inp = inputs(account=account(settled_cash=0.0, cash=0.0))
    kept, verdicts = run(
        [sell("AAPL.US", 10.0), buy("SPY.US", 1.0)], inp, positions={"AAPL.US": 10.0}
    )
    assert [o.ticker for o in kept] == ["AAPL.US"]
    assert any(v.rule == "settled_cash" and v.action == "drop" for v in verdicts)


def test_unsettled_sales_in_the_ledger_tighten_the_broker_figure():
    unsettled = SettlementEntry(
        ticker="AAPL.US",
        side="sell",
        currency="USD",
        amount=900.0,
        trade_date=AS_OF,
        settle_date=date(2026, 9, 29),
    )
    inp = inputs(account=account(cash=1_000.0, settled_cash=1_000.0), settlements=[unsettled])
    kept, _ = run([buy("AAPL.US", 5.0)], inp)
    assert kept[0].quantity == pytest.approx(1.0)  # 100 settled of our own count


def test_no_short_sale_on_a_cash_account():
    kept, verdicts = run([sell("AAPL.US", 5.0, effect="open")], inputs())
    assert kept == []
    assert verdicts[0].rule == "short_permission" and "margin" in verdicts[0].reason


def test_an_unknown_account_opens_nothing_but_closes_pass():
    inp = inputs(account=None)
    orders = [sell("AAPL.US", 2.0), buy("SPY.US", 1.0)]
    kept, verdicts = run(orders, inp, positions={"AAPL.US": 2.0})
    assert kept == [orders[0]]
    assert rules_named(verdicts) == {"account_known"}


def test_restricted_tickers_cannot_be_bought_but_can_be_sold():
    inp = inputs(restricted={"AAPL.US": "owner list"})
    orders = [buy("AAPL.US", 1.0), sell("AAPL.US", 1.0)]
    kept, verdicts = run(orders, inp, positions={"AAPL.US": 1.0})
    assert kept == [orders[1]]
    assert "owner list" in verdicts[0].reason


def test_a_close_is_never_dropped_it_needs_approval():
    """A rule that would drop a close turns it into an approval."""
    inp = inputs(restricted={"AAPL.US": "broker refused"})
    orders = [sell("AAPL.US", 1.0, effect="open")]  # an open, dropped
    kept, _ = run(orders, inp)
    assert kept == []
    from stonks.accounts.rules import AccountRule

    class DropAll(AccountRule):
        name = "drop_all"
        order = 1

        def check(self, view, qty, book, inputs, settings):
            return self.verdict(view, "drop", 0.0, "test")

    close = sell("AAPL.US", 3.0)
    kept, verdicts = run_account_rules(
        [close], inp, SETTINGS, PRICES, {"AAPL.US": 3.0}, rules=[DropAll()]
    )
    assert kept == [close]
    assert verdicts[0].action == "approve" and verdicts[0].quantity == 3.0


# ---- margin account ------------------------------------------------------------------


def test_margin_buys_fit_available_funds():
    prof = profile("us", account_type="margin")
    inp = inputs(prof, account=account(account_type="margin", available_funds=250.0))
    kept, verdicts = run([buy("AAPL.US", 5.0)], inp)
    assert kept[0].quantity == pytest.approx(2.5)
    assert rules_named(verdicts) == {"buying_power"}


def test_fx_funding_clips_a_buy_in_a_currency_not_held():
    prof = profile("uk", base_currency="GBP")
    acct = account(currency="GBP", cash_by_currency={"GBP": 10_000.0, "USD": 150.0})
    facts = {"AAPL.US": InstrumentFacts(security_type="common_stock", currency="USD")}
    kept, verdicts = run([buy("AAPL.US", 5.0)], inputs(prof, account=acct, instruments=facts))
    assert kept[0].quantity == pytest.approx(1.5)
    assert "fx_funding" in rules_named(verdicts)


# ---- US ------------------------------------------------------------------------------


def test_pdt_holds_a_fourth_day_trade_for_approval():
    prof = profile("us", account_type="margin")
    acct = account(account_type="margin", equity=10_000.0)
    trades = [date(2026, 9, 22), date(2026, 9, 23), date(2026, 9, 24)]
    inp = inputs(prof, account=acct, day_trades=trades, opened_today=frozenset({"AAPL.US"}))
    close = sell("AAPL.US", 1.0)
    kept, verdicts = run([close, buy("SPY.US", 1.0)], inp, positions={"AAPL.US": 1.0})
    assert kept == [close]  # the close goes on, the open is dropped
    actions = {(v.ticker, v.action) for v in verdicts if v.rule == "pdt"}
    assert actions == {("AAPL.US", "approve"), ("SPY.US", "drop")}


def test_pdt_trusts_the_broker_count_when_stricter():
    prof = profile("us", account_type="margin")
    acct = account(account_type="margin", day_trades_remaining=0)
    kept, verdicts = run([buy("SPY.US", 1.0)], inputs(prof, account=acct))
    assert kept == [] and verdicts[0].rule == "pdt"


def test_pdt_skips_accounts_over_the_threshold_and_cash_accounts():
    prof = profile("us", account_type="margin")
    acct = account(account_type="margin", equity=30_000.0, day_trades_remaining=0)
    kept, _ = run([buy("SPY.US", 1.0)], inputs(prof, account=acct))
    assert kept
    kept, _ = run([buy("SPY.US", 1.0)], inputs(day_trades=[AS_OF] * 5))
    assert kept


def test_wash_sale_warns_or_blocks():
    sold = {"AAPL.US": date(2026, 9, 10)}
    kept, verdicts = run([buy("AAPL.US", 1.0)], inputs(loss_sales=sold))
    assert kept and verdicts[0].action == "note" and "wash sale" in verdicts[0].reason
    blocking = profile("us", wash_sale_mode="block")
    kept, verdicts = run([buy("AAPL.US", 1.0)], inputs(blocking, loss_sales=sold))
    assert kept == [] and verdicts[0].action == "drop"
    old = {"AAPL.US": date(2026, 8, 1)}
    kept, verdicts = run([buy("AAPL.US", 1.0)], inputs(blocking, loss_sales=old))
    assert kept and not verdicts
    # wash sales switched off in the tax settings: the guard stays quiet
    off = profile("us", wash_sale_mode="block", wash_sales=False)
    kept, verdicts = run([buy("AAPL.US", 1.0)], inputs(off, loss_sales=sold))
    assert kept and not verdicts


def test_reg_sho_needs_a_locate_and_respects_the_price_test():
    prof = profile("us", account_type="margin", allow_short=True)
    acct = account(account_type="margin")
    short = sell("AAPL.US", 10.0, effect="open")
    kept, verdicts = run([short], inputs(prof, account=acct))
    assert kept == [] and "locate" in verdicts[-1].reason
    kept, _ = run([short], inputs(prof, account=acct, shortable={"AAPL.US": 4.0}))
    assert kept[0].quantity == 4.0
    kept, verdicts = run(
        [short],
        inputs(
            prof,
            account=acct,
            shortable={"AAPL.US": None},
            short_sale_restricted=frozenset({"AAPL.US"}),
        ),
    )
    assert kept == [] and "Rule 201" in verdicts[-1].reason


# ---- EU and UK -------------------------------------------------------------------------


def test_priips_blocks_a_us_etf_for_retail_and_allows_a_ucits():
    facts = {
        "SPY.US": InstrumentFacts(security_type="etf", isin="US78462F1030", currency="USD"),
        "VUSA.LSE": InstrumentFacts(security_type="etf", isin="IE00B3XXRP09", currency="GBP"),
        "AAPL.US": InstrumentFacts(security_type="common_stock", isin="US0378331005"),
    }
    prof = profile("uk", base_currency="GBP")
    acct = account(currency="GBP", cash_by_currency={"GBP": 100_000.0, "USD": 100_000.0})
    orders = [buy("SPY.US", 1.0, 1), buy("VUSA.LSE", 1.0, 2), buy("AAPL.US", 1.0, 3)]
    kept, verdicts = run(orders, inputs(prof, account=acct, instruments=facts))
    assert [o.ticker for o in kept] == ["VUSA.LSE", "AAPL.US"]
    assert [v.ticker for v in verdicts if v.rule == "priips_kid"] == ["SPY.US"]
    # a professional client, or a fund with a document, may buy it
    pro = replace(prof, client_class="professional")
    assert len(run(orders, inputs(pro, account=acct, instruments=facts))[0]) == 3
    flagged = inputs(prof, account=acct, instruments=facts, kid_available={"SPY.US": True})
    assert len(run(orders, flagged)[0]) == 3


def test_short_disclosure_keeps_the_short_under_the_reporting_line():
    prof = profile("eu", account_type="margin", allow_short=True, base_currency="EUR")
    acct = account(account_type="margin", currency="EUR")
    facts = {"SAP.XETRA": InstrumentFacts(shares_outstanding=1_000_000.0, currency="EUR")}
    short = sell("SAP.XETRA", 5_000.0, effect="open")
    kept, verdicts = run(
        [short], inputs(prof, account=acct, instruments=facts), positions={"SAP.XETRA": -400.0}
    )
    assert kept[0].quantity < 600.0 and kept[0].quantity > 599.0
    assert verdicts[0].rule == "short_disclosure"
    kept, _ = run([short], inputs(prof, account=acct))
    assert kept == []


# ---- review 2026-09-27: day trades counted from the fills ------------------------------


def _fill(side: str, qty: float, day: date, ticker: str = "X.US"):
    return (ticker, side, qty, 10.0, day)


def test_two_round_trips_in_one_day_are_two_day_trades():
    from stonks.accounts.rules.inputs import day_trades

    fills = [_fill(s, 10, AS_OF) for s in ("buy", "sell", "buy", "sell")]
    assert day_trades(fills, AS_OF, 5) == [AS_OF, AS_OF]


def test_selling_an_old_holding_then_buying_is_not_a_day_trade():
    from datetime import timedelta

    from stonks.accounts.rules.inputs import day_trades

    fills = [
        _fill("buy", 10, AS_OF - timedelta(days=7)),
        _fill("sell", 10, AS_OF),
        _fill("buy", 10, AS_OF),
    ]
    assert day_trades(fills, AS_OF, 5) == []


def test_a_short_opened_and_covered_today_is_a_day_trade_and_opened_today():
    from stonks.accounts.rules.inputs import day_trades, opened_on

    fills = [_fill("sell", 10, AS_OF), _fill("buy", 10, AS_OF)]
    assert day_trades(fills, AS_OF, 5) == [AS_OF]
    assert opened_on([_fill("sell", 10, AS_OF)], AS_OF) == frozenset({"X.US"})

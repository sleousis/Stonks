"""Margin account rules (roadmap 19.13): margin accounts are off by
default, the broker must report a margin account, and each opening order
fits IBKR's what-if margin with a safety buffer."""

from __future__ import annotations

from datetime import date

from stonks.accounts.rules import (
    AccountProfile,
    AccountRuleInputs,
    registered_account_rules,
    run_account_rules,
)
from stonks.core.types import Order
from stonks.execution.brokers.base import LiveAccountState, MarginPreview
from stonks.production.rules._account_settings import AccountRulesSettings

AS_OF = date(2026, 9, 28)
ON = AccountRulesSettings(enabled=True, margin_accounts=True, margin_buffer=0.1)
PRICES = {"AAPL.US": 100.0, "MSFT.US": 400.0}


def account(**kw) -> LiveAccountState:
    base: dict[str, object] = {
        "equity": 100_000.0,
        "cash": 100_000.0,
        "settled_cash": 100_000.0,
        # buying power does not bind here: the what-if does
        "available_funds": 10_000_000.0,
        "buying_power": 40_000_000.0,
        "currency": "USD",
        "account_type": "margin",
        "reported_type": "margin",
        "excess_liquidity": 100_000.0,
    }
    base.update(kw)
    return LiveAccountState(**base)  # type: ignore[arg-type]


def per_share(initial: float, maintenance: float, *, warning: str | None = None):
    calls: list[Order] = []

    def preview(order: Order) -> MarginPreview:
        calls.append(order)
        return MarginPreview(
            client_id=order.client_id,
            initial_margin_change=initial * order.quantity,
            maintenance_margin_change=maintenance * order.quantity,
            equity_with_loan_after=100_000.0,
            commission=1.0,
            warning=warning,
        )

    preview.calls = calls  # type: ignore[attr-defined]
    return preview


def margin_inputs(**kw) -> AccountRuleInputs:
    kw.setdefault("account", account())
    kw.setdefault("margin_preview", per_share(50.0, 25.0))
    prof = AccountProfile(portfolio_id="pf_live", jurisdiction="us", account_type="margin",
                          allow_short=True)  # fmt: skip
    return AccountRuleInputs(profile=prof, as_of=AS_OF, shortable={"AAPL.US": None}, **kw)


def buy(ticker="AAPL.US", qty=10.0, i=0) -> Order:
    return Order(client_id=f"b{i}", ticker=ticker, side="buy", quantity=qty)


def short(ticker="AAPL.US", qty=10.0, i=0) -> Order:
    return Order(client_id=f"s{i}", ticker=ticker, side="sell", quantity=qty,
                 position_effect="open")  # fmt: skip


def run(orders, inp, settings=ON, positions=None):
    return run_account_rules(orders, inp, settings, PRICES, positions or {})


def test_margin_rules_apply_only_to_margin_accounts():
    cash = AccountProfile(portfolio_id="p", jurisdiction="us")
    margin = AccountProfile(portfolio_id="p", jurisdiction="us", account_type="margin")
    assert {"margin_allowed", "margin_what_if"}.isdisjoint(
        {r.name for r in registered_account_rules(cash)}
    )
    assert {"margin_allowed", "margin_what_if"} <= {
        r.name for r in registered_account_rules(margin)
    }


def test_margin_accounts_are_off_by_default():
    assert AccountRulesSettings().margin_accounts is False
    kept, verdicts = run([buy()], margin_inputs(), AccountRulesSettings(enabled=True))
    assert kept == []
    assert verdicts[0].rule == "margin_allowed" and "off" in verdicts[0].reason


def test_off_still_lets_a_close_through_for_approval():
    sell = Order(client_id="c", ticker="AAPL.US", side="sell", quantity=5.0)
    kept, verdicts = run(
        [sell], margin_inputs(), AccountRulesSettings(enabled=True), positions={"AAPL.US": 5.0}
    )
    assert kept == [sell]
    assert all(v.action in ("approve", "note") for v in verdicts)


def test_the_broker_must_report_a_margin_account():
    for reported in ("cash", None):
        kept, verdicts = run([buy()], margin_inputs(account=account(reported_type=reported)))
        assert kept == []
        assert verdicts[0].rule == "margin_allowed"
        assert "does not report a margin account" in verdicts[0].reason


def test_an_order_within_the_what_if_margin_passes():
    inp = margin_inputs()
    kept, verdicts = run([buy(qty=10.0)], inp)
    assert kept == [buy(qty=10.0)] and verdicts == []
    assert inp.margin_preview.calls[0].quantity == 10.0  # type: ignore[union-attr]


def test_initial_margin_is_clipped_to_the_buffered_room():
    # 90,000 of room (10% buffer on 100,000), 40,000 used: 50,000 / 50 per share
    inp = margin_inputs(account=account(initial_margin=40_000.0))
    kept, verdicts = run([buy(qty=2_000.0)], inp)
    assert kept[0].quantity == 1_000.0
    assert verdicts[0].rule == "margin_what_if" and verdicts[0].action == "clip"
    assert "initial margin" in verdicts[0].reason


def test_maintenance_margin_is_checked_too():
    # maintenance: 90,000 cap, 85,000 used, 25 per share: 200 shares
    inp = margin_inputs(account=account(maintenance_margin=85_000.0))
    kept, verdicts = run([buy(qty=1_000.0)], inp)
    assert kept[0].quantity == 200.0
    assert "maintenance margin" in verdicts[0].reason


def test_the_running_margin_counts_earlier_orders_of_the_run():
    inp = margin_inputs(account=account(initial_margin=0.0))
    kept, _ = run([buy(qty=1_000.0, i=1), buy("MSFT.US", qty=1_000.0, i=2)], inp)
    # 90,000 room: the first uses 50,000, the second gets 40,000 / 50 = 800
    assert [o.quantity for o in kept] == [1_000.0, 800.0]


def test_short_sales_use_margin_as_well():
    inp = margin_inputs(account=account(initial_margin=89_000.0))
    kept, verdicts = run([short(qty=100.0)], inp)
    assert kept[0].quantity == 20.0
    assert any(v.rule == "margin_what_if" for v in verdicts)


def test_a_failed_what_if_never_lets_an_order_through():
    def broken(order: Order) -> MarginPreview:
        raise TimeoutError("what-if timed out")

    kept, verdicts = run([buy()], margin_inputs(margin_preview=broken))
    assert kept == []
    assert "what-if" in verdicts[0].reason


def test_no_what_if_means_no_open():
    kept, verdicts = run([buy()], margin_inputs(margin_preview=None))
    assert kept == [] and verdicts[0].rule == "margin_what_if"


def test_a_what_if_warning_drops_the_order():
    inp = margin_inputs(margin_preview=per_share(50.0, 25.0, warning="insufficient equity"))
    kept, verdicts = run([buy()], inp)
    assert kept == [] and "insufficient equity" in verdicts[0].reason


def test_closes_skip_the_what_if():
    inp = margin_inputs()
    sell = Order(client_id="c", ticker="AAPL.US", side="sell", quantity=5.0)
    kept, _ = run([sell], inp, positions={"AAPL.US": 5.0})
    assert kept == [sell]
    assert inp.margin_preview.calls == []  # type: ignore[union-attr]


def test_the_buffer_tightens_only():
    from stonks.production.rules.settings import tighter_rule_settings

    base = AccountRulesSettings(margin_accounts=True, margin_buffer=0.1)
    from stonks.production.rules.settings import RuleSettings

    rules = RuleSettings(account_rules=base)
    merged = tighter_rule_settings(
        rules, {"account_rules": {"margin_accounts": False, "margin_buffer": 0.05}}
    )
    assert merged.account_rules.margin_accounts is False
    assert merged.account_rules.margin_buffer == 0.1
    looser = tighter_rule_settings(
        RuleSettings(account_rules=AccountRulesSettings()),
        {"account_rules": {"margin_accounts": True}},
    )
    assert looser.account_rules.margin_accounts is False


def test_the_pdt_rule_is_in_force_under_25000_usd():
    inp = margin_inputs(account=account(equity=20_000.0, day_trades_remaining=0))
    kept, verdicts = run([buy()], inp)
    assert kept == [] and any(v.rule == "pdt" for v in verdicts)
    rich = margin_inputs(account=account(equity=30_000.0, day_trades_remaining=0))
    kept, verdicts = run([buy(qty=1.0)], rich)
    assert kept and not any(v.rule == "pdt" for v in verdicts)

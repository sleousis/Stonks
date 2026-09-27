"""Account rules for every jurisdiction (roadmap 19.7).

- ``restricted``: no opening order in a ticker on the portfolio's
  restricted list (the owner's own, and names the broker refused before);
- ``short_permission``: no short sale on a cash account, or on a margin
  account whose profile does not allow shorts;
- ``settled_cash``: a cash account buys with settled cash only. Sale
  proceeds do not count until they settle, so the account never buys with
  money it has not received yet (free-riding). No margin, ever;
- ``buying_power``: a margin account's buys fit its available funds;
- ``fx_funding``: a buy in a currency the account holds too little of is
  clipped to what it holds (``fx_policy = refuse``). A cash account never
  borrows a currency.

An account whose state could not be read opens nothing.
"""

from __future__ import annotations

from stonks.accounts.rules import (
    EPS,
    AccountBook,
    AccountRule,
    AccountRuleInputs,
    AccountRulesSettings,
    OrderView,
    Verdict,
    register_account_rule,
)


@register_account_rule
class Restricted(AccountRule):
    name = "restricted"
    order = 10

    def check(
        self,
        view: OrderView,
        qty: float,
        book: AccountBook,
        inputs: AccountRuleInputs,
        settings: AccountRulesSettings,
    ) -> Verdict | None:
        reason = inputs.restricted.get(view.ticker)
        if reason is None or not view.opening:
            return None
        return self.verdict(view, "drop", 0.0, f"{view.ticker} is restricted: {reason}")


@register_account_rule
class ShortPermission(AccountRule):
    name = "short_permission"
    order = 20

    def check(
        self,
        view: OrderView,
        qty: float,
        book: AccountBook,
        inputs: AccountRuleInputs,
        settings: AccountRulesSettings,
    ) -> Verdict | None:
        if view.side != "sell" or not view.opening:
            return None
        profile = inputs.profile
        if profile.account_type == "cash":
            return self.verdict(view, "drop", 0.0, "short sales need a margin account")
        if not profile.allow_short:
            return self.verdict(view, "drop", 0.0, "shorts are off for this account")
        return None


@register_account_rule
class AccountKnown(AccountRule):
    """Every other money rule needs the broker's numbers."""

    name = "account_known"
    order = 25

    def check(
        self,
        view: OrderView,
        qty: float,
        book: AccountBook,
        inputs: AccountRuleInputs,
        settings: AccountRulesSettings,
    ) -> Verdict | None:
        if not view.opening or inputs.account is not None:
            return None
        return self.verdict(view, "drop", 0.0, "the account could not be read; nothing opens")


@register_account_rule
class SettledCash(AccountRule):
    name = "settled_cash"
    order = 30
    account_types = frozenset({"cash"})

    def check(
        self,
        view: OrderView,
        qty: float,
        book: AccountBook,
        inputs: AccountRuleInputs,
        settings: AccountRulesSettings,
    ) -> Verdict | None:
        if view.side != "buy" or not view.opening or book.settled_cash is None:
            return None
        if view.price is None:
            return self.verdict(view, "drop", 0.0, "no price to check settled cash against")
        room = max(book.settled_cash, 0.0) / view.price
        return self.clip_to(
            view,
            qty,
            room,
            f"a cash account buys with settled cash only ({book.settled_cash:,.2f} left)",
        )


@register_account_rule
class BuyingPower(AccountRule):
    name = "buying_power"
    order = 35
    account_types = frozenset({"margin"})

    def check(
        self,
        view: OrderView,
        qty: float,
        book: AccountBook,
        inputs: AccountRuleInputs,
        settings: AccountRulesSettings,
    ) -> Verdict | None:
        if view.side != "buy" or not view.opening or book.available_funds is None:
            return None
        if view.price is None:
            return self.verdict(view, "drop", 0.0, "no price to check buying power against")
        room = max(book.available_funds, 0.0) / view.price
        return self.clip_to(
            view, qty, room, f"available funds {book.available_funds:,.2f} are used up"
        )


@register_account_rule
class FxFunding(AccountRule):
    name = "fx_funding"
    order = 40

    def check(
        self,
        view: OrderView,
        qty: float,
        book: AccountBook,
        inputs: AccountRuleInputs,
        settings: AccountRulesSettings,
    ) -> Verdict | None:
        profile = inputs.profile
        if view.side != "buy" or not view.opening or view.currency == profile.base_currency:
            return None
        # ``convert`` needs its own FX order before the buy, which is not
        # built yet: until then both policies only spend what is held.
        if view.price is None:
            return None  # the cash rules drop an unpriced buy
        held = book.cash_by_currency.get(view.currency, 0.0)
        room = max(held, 0.0) / view.price if held > EPS else 0.0
        return self.clip_to(
            view,
            qty,
            room,
            f"only {held:,.2f} {view.currency} held; buys in {view.currency} are not funded "
            f"by a conversion (fx_policy {profile.fx_policy})",
        )

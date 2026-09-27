"""US account rules (roadmap 19.7).

- ``pdt``: the pattern day trader rule for margin accounts under the
  equity threshold (25,000 USD): at most ``pdt_max_day_trades`` day trades
  in ``pdt_window_days`` business days. A close of a position opened in
  the same session is a day trade. One that would pass the limit needs
  approval (a close is never dropped). With no day trade left, opening
  orders are dropped too, since a protective stop could make a day trade.
  Ours and the broker's ``DayTradesRemaining`` are compared and the
  stricter wins. Cash accounts are not subject to it;
- ``wash_sale``: a buy within ``wash_sale_window_days`` after a loss sale
  of the same ticker is tagged (``wash_sale_mode = warn``) or dropped
  (``block``). Tax reporting stays with the broker;
- ``reg_sho``: an opening short sale needs a locate (a borrow quote), and
  a ticker under the short sale price test (Rule 201) cannot be shorted
  with our collared order, so it is dropped.
"""

from __future__ import annotations

from datetime import timedelta

from stonks.accounts.rules import (
    AccountBook,
    AccountRule,
    AccountRuleInputs,
    AccountRulesSettings,
    OrderView,
    Verdict,
    register_account_rule,
)

_US = frozenset({"us"})


def day_trades_used(
    book: AccountBook, inputs: AccountRuleInputs, settings: AccountRulesSettings
) -> int:
    """Day trades used in the window: ours, or what the broker's remaining
    count implies, whichever is higher."""
    used = book.day_trades_used
    account = inputs.account
    if account is not None and account.day_trades_remaining is not None:
        used = max(used, settings.pdt_max_day_trades - account.day_trades_remaining)
    return used


@register_account_rule
class PatternDayTrader(AccountRule):
    name = "pdt"
    order = 50
    jurisdictions = _US
    account_types = frozenset({"margin"})

    def check(
        self,
        view: OrderView,
        qty: float,
        book: AccountBook,
        inputs: AccountRuleInputs,
        settings: AccountRulesSettings,
    ) -> Verdict | None:
        account = inputs.account
        if account is not None and account.equity >= settings.pdt_equity_threshold:
            return None
        used = day_trades_used(book, inputs, settings)
        limit = settings.pdt_max_day_trades
        if not view.opening and view.ticker in inputs.opened_today:
            if used >= limit:
                return self.verdict(
                    view,
                    "approve",
                    qty,
                    f"closing {view.ticker} opened today would be day trade {used + 1} "
                    f"(limit {limit} in {settings.pdt_window_days} days)",
                )
            book.day_trades_used = used + 1
            return None
        if view.opening and used >= limit:
            return self.verdict(
                view,
                "drop",
                0.0,
                f"no day trade left ({used} of {limit}); a stop could make another",
            )
        return None


@register_account_rule
class WashSale(AccountRule):
    name = "wash_sale"
    order = 60
    jurisdictions = _US

    def check(
        self,
        view: OrderView,
        qty: float,
        book: AccountBook,
        inputs: AccountRuleInputs,
        settings: AccountRulesSettings,
    ) -> Verdict | None:
        if view.side != "buy" or not view.opening:
            return None
        sold = inputs.loss_sales.get(view.ticker)
        if sold is None or inputs.as_of - sold > timedelta(days=settings.wash_sale_window_days):
            return None
        reason = f"wash sale: {view.ticker} was sold at a loss on {sold.isoformat()}"
        if inputs.profile.wash_sale_mode == "block":
            return self.verdict(view, "drop", 0.0, reason)
        return self.verdict(view, "note", qty, reason)


@register_account_rule
class RegSho(AccountRule):
    name = "reg_sho"
    order = 70
    jurisdictions = _US

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
        if view.ticker in inputs.short_sale_restricted:
            return self.verdict(
                view, "drop", 0.0, f"{view.ticker} is under the short sale price test (Rule 201)"
            )
        if view.ticker not in inputs.shortable:
            return self.verdict(view, "drop", 0.0, "no locate: no borrow quote for a short sale")
        available = inputs.shortable[view.ticker]
        if available is None:
            return None
        return self.clip_to(view, qty, available, f"only {available} shares to borrow")

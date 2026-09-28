"""Margin account rules (roadmap 19.13). Margin accounts only.

- ``margin_allowed``: margin accounts are off by default
  (``[production.risk.rules.account_rules] margin_accounts``). While off,
  a margin book opens nothing new. On, the broker itself must report a
  margin account: a margin profile on an account the broker calls cash, or
  one whose type it does not report, opens nothing;
- ``margin_what_if``: every opening order (a buy, or a short sale) is
  priced by the broker's what-if margin. The initial and the maintenance
  margin after the order, with the run's earlier orders, must stay within
  ``1 - margin_buffer`` of the account's equity. An order that would pass
  it is clipped, or dropped when nothing fits. A what-if that fails, times
  out or warns never lets an order through.

Closes never meet these rules as a drop: the engine turns a drop of a
close into ``approve`` (P28), and ``margin_what_if`` looks at opens only.
"""

from __future__ import annotations

from dataclasses import replace

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

_MARGIN = frozenset({"margin"})


@register_account_rule
class MarginAllowed(AccountRule):
    name = "margin_allowed"
    order = 22
    account_types = _MARGIN

    def check(
        self,
        view: OrderView,
        qty: float,
        book: AccountBook,
        inputs: AccountRuleInputs,
        settings: AccountRulesSettings,
    ) -> Verdict | None:
        if not view.opening:
            return None
        if not settings.margin_accounts:
            return self.verdict(
                view, "drop", 0.0, "margin accounts are off; nothing new opens on margin"
            )
        account = inputs.account
        if account is None:
            return None  # account_known drops it
        if account.reported_type != "margin":
            said = account.reported_type or "no type"
            return self.verdict(
                view,
                "drop",
                0.0,
                f"the broker does not report a margin account (it reports {said})",
            )
        return None


@register_account_rule
class MarginWhatIf(AccountRule):
    name = "margin_what_if"
    #: Last: the broker prices the quantity every other rule left.
    order = 100
    account_types = _MARGIN

    def check(
        self,
        view: OrderView,
        qty: float,
        book: AccountBook,
        inputs: AccountRuleInputs,
        settings: AccountRulesSettings,
    ) -> Verdict | None:
        account = inputs.account
        if not view.opening or account is None or qty <= EPS:
            return None
        preview_of = inputs.margin_preview
        if preview_of is None:
            return self.verdict(view, "drop", 0.0, "no what-if margin preview from the broker")
        # the preview prices a market order at the run's reference price
        # (the decision price is set only after the risk rules)
        order = replace(view.order, quantity=qty)
        if order.limit_price is None and order.decision_price is None:
            if view.price is None:
                return self.verdict(view, "drop", 0.0, "no price to ask the what-if margin for")
            order = replace(order, decision_price=view.price)
        try:
            preview = preview_of(order)
        except Exception as exc:  # a failed what-if never lets an order through
            return self.verdict(view, "drop", 0.0, f"the what-if margin preview failed: {exc}")
        if preview.warning:
            return self.verdict(
                view, "drop", 0.0, f"the broker refused it in the what-if: {preview.warning}"
            )
        initial = max(preview.initial_margin_change, 0.0) / qty
        maintenance = max(preview.maintenance_margin_change, 0.0) / qty
        book.margin_per_share[view.order.client_id] = (initial, maintenance)
        keep = 1.0 - settings.margin_buffer
        equity_after = preview.equity_with_loan_after
        if equity_after <= 0:
            equity_after = account.equity
        limits: list[tuple[float, str]] = []
        if initial > EPS:
            room = keep * equity_after - book.initial_margin
            limits.append(
                (
                    room / initial,
                    f"initial margin would pass {keep:.0%} of equity ({max(room, 0.0):,.2f} left)",
                )
            )
        if maintenance > EPS:
            room = keep * account.equity - book.maintenance_margin
            limits.append(
                (
                    room / maintenance,
                    f"maintenance margin would pass {keep:.0%} of equity "
                    f"({max(room, 0.0):,.2f} left)",
                )
            )
        if not limits:
            return None
        most, why = min(limits, key=lambda item: item[0])
        return self.clip_to(view, qty, most, why)

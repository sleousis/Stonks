"""The account rules engine as a risk rule (roadmap 19.7).

For a live book, every order goes through the account rules of its
portfolio's profile (``stonks.accounts.rules``): US pattern day trader,
settled cash, wash sales and Reg SHO, EU and UK key information documents
and short disclosure, and for every account the restricted list, short
permission, buying power and currency funding.

A live book with no account profile, or no inputs, opens nothing. Each
verdict becomes a ``RiskAdjustment`` tagged ``account_rules.<rule>``. A
close is never dropped or shrunk (P28): the engine turns that into
``approve``, which is recorded here with an unchanged quantity so the
submit step (roadmap 19.8) can hold it for a person. Runs between
``cash_buffer`` and ``min_order_notional``. Acts only on live books. Off by
default (``[production.risk.rules.account_rules] enabled``).
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

from stonks.accounts.rules import AccountRulesSettings, Verdict, run_account_rules
from stonks.core.types import Order
from stonks.production.live.quotes import reference_price
from stonks.production.rules import RiskAdjustment, RiskContext, RiskRule, register_rule
from stonks.production.rules._common import adjustment, is_opening_order, settings_of

#: The tag prefix of every account rule adjustment.
TAG = "account_rules"


def tag(rule: str) -> str:
    return f"{TAG}.{rule}"


@register_rule
class AccountRules(RiskRule):
    name = "account_rules"
    order = 65

    def enabled(self, policy: Any) -> bool:
        settings = settings_of(policy, self.name)
        return settings is not None and settings.active

    def apply(
        self, orders: Sequence[Order], ctx: RiskContext
    ) -> tuple[list[Order], list[RiskAdjustment]]:
        settings: AccountRulesSettings | None = settings_of(ctx.policy, self.name)
        live = ctx.live
        if settings is None or not settings.active or live is None:
            return list(orders), []
        inputs = live.account_rules
        if inputs is None:
            kept = [o for o in orders if not is_opening_order(o, ctx)]
            dropped = [o for o in orders if is_opening_order(o, ctx)]
            reason = "no account profile for this live portfolio; nothing opens"
            return kept, [adjustment(o, tag("profile"), 0.0, reason) for o in dropped]
        prices: dict[str, float] = {}
        for order in orders:
            ref = reference_price(ctx, order.ticker)
            if ref is not None:
                prices[order.ticker] = ref.price
        kept, verdicts = run_account_rules(
            orders, inputs, settings, prices, ctx.portfolio.positions
        )
        return kept, [_adjustment(v, orders) for v in verdicts]


def _adjustment(verdict: Verdict, orders: Sequence[Order]) -> RiskAdjustment:
    order = next((o for o in orders if o.ticker == verdict.ticker and o.side == verdict.side), None)
    reason = f"needs approval: {verdict.reason}" if verdict.action == "approve" else verdict.reason
    if order is None:  # pragma: no cover - every verdict names one of the orders
        return RiskAdjustment(
            ticker=verdict.ticker,
            side=verdict.side,
            rule=tag(verdict.rule),
            original_quantity=verdict.original_quantity,
            adjusted_quantity=verdict.quantity,
            reason=reason,
        )
    return adjustment(order, tag(verdict.rule), verdict.quantity, reason)

"""Shared machinery of the option risk rules (roadmap 17.4).

Option orders reach a rule as plain :class:`Order` legs whose ticker is a
contract id and whose ``decision_context`` names the combo they belong to
(``stonks.options.orders.ComboOrder.leg_orders``). A combo is checked and
dropped as one unit: all of its legs stay or none do, so a rule never
leaves half a spread.

- Orders with no option leg pass through untouched (these rules only look
  at options), and so does every unit that only closes positions. Closing
  units are taken first, judged against the starting book.
- ``ctx.options`` carries the day's :class:`~stonks.options.risk.
  OptionRiskView` (contracts, Greeks, marks, spots, groups). Without it an
  opening option unit is dropped: its risk cannot be measured.
- Option prices in ``ctx.prices`` and ``ctx.portfolio`` are per contract
  (per-share mark x multiplier), so ``total_value`` is the book's equity.

Rules only ever drop opening units, so they never add exposure (P28).
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

from stonks.core.options import is_option_id
from stonks.core.types import Order
from stonks.options.orders import combo_id_of
from stonks.options.risk import OptionRiskView, current_value
from stonks.production.rules import EPS, RiskAdjustment, RiskContext, RiskRule
from stonks.production.rules._common import settings_of

Unit = list[Order]


def units(orders: Sequence[Order]) -> list[Unit]:
    """Orders grouped by combo, in the order each combo first appears."""
    grouped: dict[str, Unit] = {}
    for order in orders:
        grouped.setdefault(combo_id_of(order), []).append(order)
    return list(grouped.values())


def is_option_unit(unit: Unit, view: OptionRiskView | None) -> bool:
    for order in unit:
        if view is not None and view.contract(order.ticker) is not None:
            return True
        if is_option_id(order.ticker):
            return True
    return False


def signed(order: Order) -> float:
    return order.quantity if order.side == "buy" else -order.quantity


def unit_positions(unit: Unit) -> dict[str, float]:
    out: dict[str, float] = {}
    for order in unit:
        out[order.ticker] = out.get(order.ticker, 0.0) + signed(order)
    return out


def closes_only(unit: Unit, book: Mapping[str, float]) -> bool:
    """Every leg shrinks a position without crossing zero."""
    left = dict(book)
    for order in unit:
        held = left.get(order.ticker, 0.0)
        qty = signed(order)
        if held * qty >= 0 or abs(qty) > abs(held) + EPS:
            return False
        left[order.ticker] = held + qty
    return True


def apply_unit(book: dict[str, float], unit: Unit) -> None:
    for order in unit:
        new = book.get(order.ticker, 0.0) + signed(order)
        if abs(new) <= EPS:
            book.pop(order.ticker, None)
        else:
            book[order.ticker] = new


def unit_cost(unit: Unit, view: OptionRiskView) -> float | None:
    """Cash the unit is expected to take at the marks (negative: a credit)."""
    return current_value(unit_positions(unit), view)


def equity(ctx: RiskContext) -> float:
    return ctx.portfolio.total_value(dict(ctx.prices))


class OptionRiskRule(RiskRule):
    """Base of the option rules. Subclasses implement :meth:`check_unit`."""

    #: Off by default; the settings switch a rule on.
    settings_key: str = ""

    def settings(self, policy: Any) -> Any:
        return settings_of(policy, self.settings_key or self.name)

    def enabled(self, policy: Any) -> bool:
        settings = self.settings(policy)
        return settings is not None and bool(settings.active)

    def check_unit(
        self,
        unit: Unit,
        book: Mapping[str, float],
        ctx: RiskContext,
        view: OptionRiskView,
        settings: Any,
    ) -> str | None:
        """Why the opening ``unit`` is refused, or ``None`` to keep it.
        ``book`` holds the positions after the units kept so far."""
        raise NotImplementedError

    def apply(
        self, orders: Sequence[Order], ctx: RiskContext
    ) -> tuple[list[Order], list[RiskAdjustment]]:
        settings = self.settings(ctx.policy)
        if settings is None or not settings.active:
            return list(orders), []
        view: OptionRiskView | None = getattr(ctx, "options", None)
        book = dict(ctx.portfolio.positions)
        kept: list[Order] = []
        adjustments: list[RiskAdjustment] = []
        # Exits first, judged against the starting book (as the order rules
        # run sells first): a closing combo is never dropped because an
        # opening one earlier in the list already moved the position.
        start = dict(book)
        exits = [u for u in units(orders) if closes_only(u, start)]
        exit_ids = {id(u[0]) for u in exits}
        for unit in exits:
            kept.extend(unit)
            apply_unit(book, unit)
        for unit in units(orders):
            if id(unit[0]) in exit_ids:
                continue
            if not is_option_unit(unit, view):
                kept.extend(unit)
                apply_unit(book, unit)
                continue
            if view is None:
                reason: str | None = "no option market data to measure the risk"
            else:
                reason = self.check_unit(unit, book, ctx, view, settings)
            if reason is None:
                kept.extend(unit)
                apply_unit(book, unit)
                continue
            for order in unit:
                adjustments.append(
                    RiskAdjustment(
                        ticker=order.ticker,
                        side=order.side,
                        rule=self.name,
                        original_quantity=order.quantity,
                        adjusted_quantity=0.0,
                        reason=reason,
                    )
                )
        return kept, adjustments

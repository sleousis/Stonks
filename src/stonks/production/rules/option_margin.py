"""Option margin (roadmap 17.4): the book's option margin requirement must
fit in its cash after the unit's premium.

``method`` picks the estimate (``stonks.options.risk``):

- ``reg_t``: strategy-based, per position group (a vertical needs its
  width, a cash-secured put its strike, a covered call nothing);
- ``risk_based``: the worst loss over the scenario moves.

``cash_buffer`` keeps that share of equity free on top. An opening unit is
dropped when the requirement after it exceeds the cash left after paying
(or receiving) its premium, unless it lowers the requirement. Off by
default.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from stonks.options.risk import OptionRiskView, book_requirement
from stonks.production.rules import RiskContext, register_rule
from stonks.production.rules._options import (
    OptionRiskRule,
    Unit,
    apply_unit,
    equity,
    unit_cost,
    unit_positions,
)


class OptionMarginSettings(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    enabled: bool = False
    method: Literal["reg_t", "risk_based"] = "reg_t"
    cash_buffer: float = Field(default=0.0, ge=0.0, le=1.0)

    @property
    def active(self) -> bool:
        return self.enabled


@register_rule
class OptionMargin(OptionRiskRule):
    name = "option_margin"
    order = 9

    def check_unit(
        self,
        unit: Unit,
        book: Mapping[str, float],
        ctx: RiskContext,
        view: OptionRiskView,
        settings: Any,
    ) -> str | None:
        s: OptionMarginSettings = settings
        cost = unit_cost(unit, view)
        if cost is None:
            return "no mark to price the unit"
        after = dict(book)
        apply_unit(after, unit)
        groups = [*view.groups, unit_positions(unit)]
        before_req = book_requirement(dict(book), view, view.groups, method=s.method)
        after_req = book_requirement(after, view, groups, method=s.method)
        cash_after = ctx.portfolio.cash - _spent(book, ctx) - cost
        room = cash_after - s.cash_buffer * equity(ctx)
        if cash_after < -1e-6:
            return f"premium {cost:.2f} exceeds cash {cash_after + cost:.2f}"
        if after_req > room + 1e-6 and after_req > before_req + 1e-6:
            return f"{s.method} requirement {after_req:.2f} over free cash {room:.2f}"
        return None


def _spent(book: Mapping[str, float], ctx: RiskContext) -> float:
    """Cash the units kept earlier in this pass take: the book value moved
    from cash into positions since the start of the pass."""
    start = ctx.portfolio.positions
    spent = 0.0
    for instrument in set(book) | set(start):
        delta = book.get(instrument, 0.0) - start.get(instrument, 0.0)
        if delta:
            spent += delta * ctx.prices.get(instrument, 0.0)
    return spent

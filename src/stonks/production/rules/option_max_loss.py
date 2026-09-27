"""Max loss per option structure (roadmap 17.4; design section 5).

Every opening option unit must have a defined max loss:

- ``max_loss_per_group``: its max loss at most this share of equity;
- ``max_loss_total``: the max losses of every option group in the book,
  this unit included, at most this share of equity.

Max loss is measured from the unit's cost at the marks
(``stonks.options.risk.max_loss``). Short calls may be covered by shares
the book holds and has not pledged to other short calls: the loss counted
is then what the unit adds to holding those shares (a covered call adds
none). A unit with unbounded loss is always dropped while the rule is on.
Off by default.
"""

from __future__ import annotations

import math
from collections.abc import Mapping
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from stonks.options.risk import OptionRiskView, current_value, max_loss
from stonks.production.rules import RiskContext, register_rule
from stonks.production.rules._options import OptionRiskRule, Unit, equity, unit_positions


class OptionMaxLossSettings(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    max_loss_per_group: float | None = Field(default=None, ge=0.0)
    max_loss_total: float | None = Field(default=None, ge=0.0)

    @property
    def active(self) -> bool:
        return self.max_loss_per_group is not None or self.max_loss_total is not None


def free_cover(book: Mapping[str, float], view: OptionRiskView, underlying: str) -> float:
    """Shares of ``underlying`` held and not pledged to short calls."""
    held = book.get(underlying, 0.0)
    pledged = 0.0
    for instrument, qty in book.items():
        contract = view.contract(instrument)
        if contract is not None and contract.underlying == underlying and contract.is_call:
            pledged -= qty * contract.multiplier  # short calls pledge, long calls free
    return max(held - max(pledged, 0.0), 0.0)


def incremental_max_loss(
    positions: Mapping[str, float], book: Mapping[str, float], view: OptionRiskView
) -> float:
    """Max loss of ``positions`` given the book's free shares as cover."""
    base = max_loss(positions, view)
    if not math.isinf(base):
        return base
    cover: dict[str, float] = {}
    for instrument, qty in positions.items():
        contract = view.contract(instrument)
        if contract is None or not contract.is_call or qty >= 0:
            continue
        need = -qty * contract.multiplier
        free = free_cover(book, view, contract.underlying) - cover.get(contract.underlying, 0.0)
        cover[contract.underlying] = cover.get(contract.underlying, 0.0) + min(need, free)
    if not cover:
        return math.inf
    covered = dict(positions)
    for underlying, qty in cover.items():
        covered[underlying] = covered.get(underlying, 0.0) + qty
    shares_value = current_value(cover, view)
    if shares_value is None:
        return math.inf
    with_cover = max_loss(covered, view)
    return max(with_cover - shares_value, 0.0)


@register_rule
class OptionMaxLoss(OptionRiskRule):
    name = "option_max_loss"
    order = 9

    def check_unit(
        self,
        unit: Unit,
        book: Mapping[str, float],
        ctx: RiskContext,
        view: OptionRiskView,
        settings: Any,
    ) -> str | None:
        s: OptionMaxLossSettings = settings
        value = equity(ctx)
        loss = incremental_max_loss(unit_positions(unit), book, view)
        if math.isinf(loss):
            return "unbounded or unknown max loss"
        if s.max_loss_per_group is not None and loss > s.max_loss_per_group * value:
            return f"max loss {loss:.2f} over {s.max_loss_per_group:.2%} of equity {value:.2f}"
        if s.max_loss_total is not None:
            total = loss
            for group in view.groups:
                if not any(view.contract(k) is not None for k in group):
                    continue
                total += incremental_max_loss(group, book, view)
            if total > s.max_loss_total * value:
                return (
                    f"book max loss {total:.2f} over {s.max_loss_total:.2%} of equity {value:.2f}"
                )
        return None

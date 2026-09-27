"""Short option guard (roadmap 17.4): what a book may sell, by the
broker's options approval level, and never a naked short call in v1.

Levels (as brokers such as IBKR grant them):

1. covered calls;
2. plus long calls and puts, protective puts and cash-secured puts;
3. plus spreads (verticals, iron condors);
4. naked shorts, which v1 still refuses.

An opening unit is dropped when:

- it sells a call not covered by a long call in the unit or by free
  shares in the book (``naked short call``);
- it sells a put not covered by a long put in the unit and the cash left
  cannot secure the strike (``cash_secured`` is on by default);
- its shape needs a higher level than ``approval_level``;
- a short leg expires in fewer than ``min_dte`` days;
- it would take the short contracts on one underlying above
  ``max_short_contracts``.

Off by default (``enabled``).
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from stonks.core.options import OptionContract
from stonks.options.risk import OptionRiskView
from stonks.production.rules import RiskContext, register_rule
from stonks.production.rules._options import (
    OptionRiskRule,
    Unit,
    unit_cost,
    unit_positions,
)
from stonks.production.rules.option_max_loss import free_cover


class ShortOptionGuardSettings(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    enabled: bool = False
    approval_level: int = Field(default=2, ge=1, le=4)
    cash_secured: bool = True
    min_dte: int = Field(default=0, ge=0)
    max_short_contracts: float | None = Field(default=None, ge=0.0)

    @property
    def active(self) -> bool:
        return self.enabled


def reserved_for_puts(book: Mapping[str, float], view: OptionRiskView) -> float:
    total = 0.0
    for instrument, qty in book.items():
        contract = view.contract(instrument)
        if contract is not None and contract.right == "put" and qty < 0:
            total += -qty * contract.strike * contract.multiplier
    return total


def level_needed(options: list[tuple[OptionContract, float]], covered_calls: bool) -> int:
    shorts = [(c, q) for c, q in options if q < 0]
    longs = [(c, q) for c, q in options if q > 0]
    if not shorts:
        return 2
    paired = any(lc.right == sc.right for sc, _ in shorts for lc, _ in longs)
    if paired or len(shorts) > 1:
        return 3
    only_short = shorts[0][0]
    if only_short.is_call and covered_calls and not longs:
        return 1
    return 2


@register_rule
class ShortOptionGuard(OptionRiskRule):
    name = "short_option_guard"
    order = 9

    def check_unit(
        self,
        unit: Unit,
        book: Mapping[str, float],
        ctx: RiskContext,
        view: OptionRiskView,
        settings: Any,
    ) -> str | None:
        s: ShortOptionGuardSettings = settings
        positions = unit_positions(unit)
        options = [(c, q) for k, q in positions.items() if (c := view.contract(k)) is not None]
        for contract, qty in options:
            if qty < 0 and contract.days_to_expiry(view.as_of) < s.min_dte:
                return f"short {contract.contract_id} expires within {s.min_dte} days"
        covered_calls = True
        for underlying in {c.underlying for c, _ in options}:
            shorts = sum(
                -q * c.multiplier
                for c, q in options
                if c.underlying == underlying and c.is_call and q < 0
            )
            longs = sum(
                q * c.multiplier
                for c, q in options
                if c.underlying == underlying and c.is_call and q > 0
            )
            unit_shares = max(positions.get(underlying, 0.0), 0.0)
            cover = longs + unit_shares + free_cover(book, view, underlying)
            if shorts > cover + 1e-9:
                return f"naked short call on {underlying}"
            if shorts > unit_shares + free_cover(book, view, underlying) + 1e-9:
                covered_calls = False
            if s.max_short_contracts is not None:
                held = sum(
                    -q
                    for k, q in book.items()
                    if (c := view.contract(k)) is not None and c.underlying == underlying and q < 0
                )
                new = sum(-q for c, q in options if c.underlying == underlying and q < 0)
                if held + new > s.max_short_contracts + 1e-9:
                    return f"{held + new:g} short contracts on {underlying} over {s.max_short_contracts:g}"
        need = level_needed(options, covered_calls)
        if need > s.approval_level:
            return f"needs options level {need}, approved for {s.approval_level}"
        if s.cash_secured:
            uncovered = 0.0
            for contract, qty in options:
                if contract.right != "put" or qty >= 0:
                    continue
                long_puts = sum(
                    q
                    for c, q in options
                    if c.right == "put"
                    and q > 0
                    and c.underlying == contract.underlying
                    and c.expiry == contract.expiry
                )
                naked = max(-qty - long_puts, 0.0)
                uncovered += naked * contract.strike * contract.multiplier
            if uncovered > 0:
                cost = unit_cost(unit, view) or 0.0
                free = ctx.portfolio.cash - cost - reserved_for_puts(book, view)
                if uncovered > free + 1e-6:
                    return f"put needs {uncovered:.2f} cash secured, {free:.2f} free"
        return None

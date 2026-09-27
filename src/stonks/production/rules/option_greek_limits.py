"""Option Greek limits (roadmap 17.4; Natenberg, Sinclair): caps on the
book's Greeks as a share of equity, for the whole book and per position.

Portfolio limits (all optional, fractions of equity):

- ``max_dollar_delta``: absolute dollar delta (shares and options);
- ``max_dollar_gamma``: absolute change in dollar delta for a 1% move;
- ``max_vega``: absolute dollars per vol point;
- ``max_theta``: absolute dollars of time decay per day.

Per position: ``max_position_dollar_delta`` and ``max_position_vega``.

An opening option unit is dropped when, after it, a limit is exceeded and
the unit made that Greek larger (a unit that shrinks an existing breach
may trade). A unit whose Greeks are unknown is dropped. Off by default.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from stonks.options.risk import OptionRiskView, portfolio_greeks, position_greeks
from stonks.production.rules import RiskContext, register_rule
from stonks.production.rules._options import (
    OptionRiskRule,
    Unit,
    apply_unit,
    equity,
    unit_positions,
)


class OptionGreekLimitsSettings(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    max_dollar_delta: float | None = Field(default=None, ge=0.0)
    max_dollar_gamma: float | None = Field(default=None, ge=0.0)
    max_vega: float | None = Field(default=None, ge=0.0)
    max_theta: float | None = Field(default=None, ge=0.0)
    max_position_dollar_delta: float | None = Field(default=None, ge=0.0)
    max_position_vega: float | None = Field(default=None, ge=0.0)

    @property
    def active(self) -> bool:
        return any(v is not None for v in self.model_dump().values())


_BOOK = {
    "max_dollar_delta": "dollar_delta",
    "max_dollar_gamma": "dollar_gamma",
    "max_vega": "vega",
    "max_theta": "theta",
}


@register_rule
class OptionGreekLimits(OptionRiskRule):
    name = "option_greek_limits"
    order = 9

    def check_unit(
        self,
        unit: Unit,
        book: Mapping[str, float],
        ctx: RiskContext,
        view: OptionRiskView,
        settings: Any,
    ) -> str | None:
        s: OptionGreekLimitsSettings = settings
        value = equity(ctx)
        after = dict(book)
        apply_unit(after, unit)
        touched = set(unit_positions(unit))
        before_g, _ = portfolio_greeks(dict(book), view)
        after_g, unknown = portfolio_greeks(after, view)
        missing = sorted(touched & set(unknown))
        if missing:
            return f"Greeks unknown for {', '.join(missing)}"
        for limit_name, attr in _BOOK.items():
            limit = getattr(s, limit_name)
            if limit is None:
                continue
            new, old = abs(getattr(after_g, attr)), abs(getattr(before_g, attr))
            if new > limit * value and new > old + 1e-9:
                return f"book {attr} {new:.2f} over {limit:.2%} of equity {value:.2f}"
        for instrument in sorted(touched):
            qty_after = after.get(instrument, 0.0)
            pg = position_greeks(instrument, qty_after, view)
            old = position_greeks(instrument, book.get(instrument, 0.0), view)
            if pg is None or old is None:
                continue
            for limit, attr in (
                (s.max_position_dollar_delta, "dollar_delta"),
                (s.max_position_vega, "vega"),
            ):
                if limit is None:
                    continue
                new_v, old_v = abs(getattr(pg, attr)), abs(getattr(old, attr))
                if new_v > limit * value and new_v > old_v + 1e-9:
                    return f"{instrument} {attr} {new_v:.2f} over {limit:.2%} of equity {value:.2f}"
        return None

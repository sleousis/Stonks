"""A stop written on the opening order itself: the trade plan of the manual
ticket (roadmap 23.4). Read from the order's decision context: the manual
ticket writes ``stop_price`` and ``target_price`` at the top level. A
``plan.stop`` (or ``plan.stop_price``) with ``plan.target``, and a
top-level ``stop_loss``, also count."""

from __future__ import annotations

import math
from collections.abc import Mapping
from typing import TYPE_CHECKING, Any

from stonks.journal.stops import FoundStop, StopSource, register_stop_source

if TYPE_CHECKING:
    from stonks.journal.trips import Ledger, LedgerOrder


def _price(value: Any) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) and number > 0 else None


@register_stop_source
class OrderPlanStop(StopSource):
    name = "order_plan"
    priority = 10

    def find(self, entry: LedgerOrder, ledger: Ledger) -> FoundStop | None:
        context = entry.context
        plan = context.get("plan")
        if isinstance(plan, Mapping):
            stop = _price(plan.get("stop", plan.get("stop_price")))
            if stop is not None:
                return FoundStop(stop, self.name, target=_price(plan.get("target")))
        stop = _price(context.get("stop_price"))
        if stop is not None:
            return FoundStop(stop, self.name, target=_price(context.get("target_price")))
        stop = _price(context.get("stop_loss"))
        return FoundStop(stop, self.name) if stop is not None else None

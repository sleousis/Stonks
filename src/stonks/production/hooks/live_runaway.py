"""Open the ``runaway`` halt after a live run with too many closes (roadmap 19.6).

``max_orders_per_run`` is a pure rule: it tags the closes of a runaway run
with a ``runaway`` adjustment. This portfolio hook reads the book's
adjustments and opens the portfolio's ``runaway`` halt (buys) through
:func:`stonks.production.live.runaway.trip_runaway`. Not run in a dry run.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from stonks.production.hooks import PortfolioHookContext, PostTickHook, register_hook


@register_hook
class LiveRunaway(PostTickHook):
    name = "live_runaway"
    stage = "portfolio"
    order = 5

    def run(self, ctx: PortfolioHookContext) -> Mapping[str, Any] | None:
        if ctx.pipeline is None:
            return None
        from stonks.production.halts import halts_enabled
        from stonks.production.live.runaway import trip_runaway

        if not halts_enabled(ctx.state):
            return None
        halt = trip_runaway(ctx.state, ctx.portfolio_id, ctx.pipeline.adjustments, on=ctx.as_of)
        if halt is None:
            return None
        return {"runaway_halts": {ctx.portfolio_id: halt.id}}

"""TCA benchmarks as a ``tick``-stage hook (BL-32): after every tick, fill
the next session's open and close of earlier orders whose next bar is now in
the lake (see :func:`stonks.production.tca.refresh_benchmarks`). Skipped in a
dry run. It logs what it updated and leaves the tick summary alone."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from stonks.production.hooks import PostTickHook, TickHookContext, register_hook
from stonks.production.tca import refresh_benchmarks


@register_hook
class TcaBenchmarksHook(PostTickHook):
    name = "tca_benchmarks"
    stage = "tick"
    order = 80

    def run(self, ctx: TickHookContext) -> Mapping[str, Any] | None:
        if ctx.dry_run:
            return None
        refresh_benchmarks(ctx.state, ctx.lake)
        return None

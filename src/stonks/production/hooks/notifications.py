"""Notification enqueue (placeholder until S4/S6 wire the outbox).

The tick records a :class:`~stonks.production.hooks.NotifySignal` per
notify-mode subscription; this hook is where they will be written to the
notification outbox. Today it only logs how many there are and changes
nothing (no summary keys, no rows).
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from stonks.logging import get_logger
from stonks.production.hooks import PostTickHook, TickHookContext, register_hook

_log = get_logger("stonks.production.hooks.notifications")


@register_hook
class EnqueueNotifications(PostTickHook):
    name = "notification_enqueue"
    stage = "tick"
    order = 50

    def run(self, ctx: TickHookContext) -> Mapping[str, Any] | None:
        if ctx.notify_signals:
            _log.info(
                "tick.notify_signals.pending",
                tick_id=ctx.tick_id,
                count=len(ctx.notify_signals),
                dry_run=ctx.dry_run,
            )
        return None

"""Notification enqueue: the tick's notify-mode signals into the outbox.

The tick records a :class:`~stonks.production.hooks.NotifySignal` per
notify-mode subscription. This hook turns each subscribed strategy's top
picks into ``entry`` signal events through the notification router
(:func:`stonks.notify.router.notify_signals`), which fans them out to the
strategy's notify subscribers and queues deliveries for the
``DeliveryWorker``; the tick never waits on a push service. Signals are
idempotent per strategy, ticker, kind and day, so a re-run tick queues
nothing twice. Dry runs queue nothing.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from stonks.logging import get_logger
from stonks.production.hooks import PostTickHook, TickHookContext, register_hook

_log = get_logger("stonks.production.hooks.notifications")

#: Picks per strategy announced as entry signals (best first).
MAX_PICKS = 3


@register_hook
class EnqueueNotifications(PostTickHook):
    name = "notification_enqueue"
    stage = "tick"
    order = 50

    def run(self, ctx: TickHookContext) -> Mapping[str, Any] | None:
        if ctx.dry_run or not ctx.notify_signals:
            return None
        from stonks.notify.channels import build_channels
        from stonks.notify.router import NotificationRouter, SignalNotice, notify_signals
        from stonks.notify.settings import NotifySettings

        picks: dict[str, tuple[tuple[str, float], ...]] = {}
        for signal in ctx.notify_signals:
            picks.setdefault(signal.strategy_id, signal.picks)
        as_of = ctx.as_of.isoformat()
        notices = [
            SignalNotice(strategy_id, ticker, "entry", as_of)
            for strategy_id, ranked in picks.items()
            for ticker, _ in ranked[:MAX_PICKS]
        ]
        notify = NotifySettings.from_env()
        router = NotificationRouter(
            ctx.state, build_channels(notify), notify.outbox, secrets=notify.secrets
        )
        results = notify_signals(router, notices)
        queued = sum(len(r.notification_ids) for r in results)
        _log.info("tick.notify_signals.queued", tick_id=ctx.tick_id, signals=len(notices),
                  queued=queued)  # fmt: skip
        return {"notifications": {"signals": len(notices), "queued": queued}}

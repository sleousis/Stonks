"""Notification enqueue: the tick's notify-mode signals into the outbox.

The tick records a :class:`~stonks.production.hooks.NotifySignal` per
notify-mode subscription. This hook sends each subscribed strategy's
stored signal events of the day (``production.signals``: entry, exit,
increase, decrease, strongest first, with their plain reason) through the
notification router. A strategy with no stored signals (an old state
file) falls back to its top picks as ``entry`` events. Either way it goes
through the router
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
        from stonks.notify.router import SignalNotice, configured_router, notify_signals
        from stonks.production.signals import (
            events_for,
            signals_recorded,
            strategies_with_signals,
        )

        picks: dict[str, tuple[tuple[str, float], ...]] = {}
        for signal in ctx.notify_signals:
            picks.setdefault(signal.strategy_id, signal.picks)
        as_of = ctx.as_of.isoformat()
        stored = (
            strategies_with_signals(ctx.state, ctx.as_of) if signals_recorded(ctx.state) else set()
        )
        per_strategy: dict[str, int] = {}
        notices = []
        for event in events_for(ctx.state, ctx.as_of, sorted(set(picks) & stored)):
            if event.kind == "risk" or per_strategy.get(event.strategy_id, 0) >= MAX_PICKS:
                continue
            per_strategy[event.strategy_id] = per_strategy.get(event.strategy_id, 0) + 1
            notices.append(
                SignalNotice(
                    event.strategy_id, event.ticker, event.kind, as_of, reason=event.text or None
                )
            )
        notices += [
            SignalNotice(strategy_id, ticker, "entry", as_of)
            for strategy_id, ranked in picks.items()
            if strategy_id not in stored
            for ticker, _ in ranked[:MAX_PICKS]
        ]
        router = configured_router(ctx.state)
        results = notify_signals(router, notices)
        queued = sum(len(r.notification_ids) for r in results)
        _log.info("tick.notify_signals.queued", tick_id=ctx.tick_id, signals=len(notices),
                  queued=queued)  # fmt: skip
        return {"notifications": {"signals": len(notices), "queued": queued}}

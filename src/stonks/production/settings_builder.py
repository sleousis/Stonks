"""The one place that turns ``Settings`` into what a production tick runs
with, so every entrypoint (``stonks tick``, the REST API, ...) trades under
the same risk policy, shadow switch, alert routing and broker."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

from stonks.config import Settings
from stonks.notify import Notifier, build_notifier
from stonks.production.tick import BrokerFactory, TickSettings


@dataclass(frozen=True)
class TickRuntime:
    settings: TickSettings
    notifier: Notifier
    #: None for the default simulated broker (the tick builds it itself).
    broker_factory: BrokerFactory | None = None


def build_tick_settings(settings: Settings, universe: Sequence[str]) -> TickSettings:
    p = settings.production
    return TickSettings(
        universe=list(universe),
        threshold=p.threshold,
        initial_cash=p.initial_cash,
        slippage_bps=p.slippage_bps,
        fee_per_trade=p.fee_per_trade,
        max_price_staleness_days=p.max_price_staleness_days,
        risk=p.risk,
        shadow_enabled=p.shadow_enabled,
        broker_kind=settings.brokers.kind,
    )


def build_tick_runtime(settings: Settings, universe: Sequence[str]) -> TickRuntime:
    return TickRuntime(
        settings=build_tick_settings(settings, universe),
        notifier=build_notifier(settings.notify),
    )

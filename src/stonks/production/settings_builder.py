"""The one place that turns ``Settings`` into what a production tick runs
with, so every entrypoint (``stonks tick``, the REST API, ...) trades under
the same risk policy, shadow switch, alert routing and broker."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

from stonks.config import Settings
from stonks.core.protocols import Broker
from stonks.core.types import Portfolio
from stonks.execution.brokers import SimulatedCosts, make_broker
from stonks.notify import Notifier, notifier_from_settings
from stonks.production.tick import BrokerFactory, TickPlan, TickSettings, load_tick_plan
from stonks.store.state import SqliteState


@dataclass(frozen=True)
class TickRuntime:
    settings: TickSettings
    notifier: Notifier
    #: None for the default simulated broker (the tick builds it itself).
    broker_factory: BrokerFactory | None = None
    #: ``[production].books_from_subscriptions``.
    books_from_subscriptions: bool = False

    def plan_for(self, state: SqliteState) -> TickPlan | None:
        """The books to trade: one per portfolio from its subscriptions when
        ``books_from_subscriptions`` is on, else ``None`` (the tick's
        default single book over every active strategy)."""
        if not self.books_from_subscriptions:
            return None
        return load_tick_plan(state, self.settings)


def build_tick_settings(settings: Settings, universe: Sequence[str]) -> TickSettings:
    """Simulated fill costs follow ``SimulatedCosts.from_settings``:
    ``[backtest.costs]`` when configured (legacy ``[production]``
    ``slippage_bps`` / ``fee_per_trade`` then ignored), else the legacy pair."""
    p = settings.production
    costs = SimulatedCosts.from_settings(settings)
    return TickSettings(
        universe=list(universe),
        threshold=p.threshold,
        initial_cash=p.initial_cash,
        slippage_bps=costs.slippage_bps,
        fee_per_trade=costs.fee_per_trade,
        costs=costs.model,
        max_price_staleness_days=p.max_price_staleness_days,
        risk=p.risk,
        shadow_enabled=p.shadow_enabled,
        broker_kind=settings.brokers.kind,
        dividend_withholding_rate=p.dividend_withholding_rate,
        construction=p.construction,
        model_books=p.model_books,
        quit_rule=p.quit_rule,
    )


def build_tick_runtime(settings: Settings, universe: Sequence[str]) -> TickRuntime:
    """The simulated default gets no factory (the tick builds its in-memory
    broker, no keys needed). ``alpaca`` is strictly opt-in via
    ``[brokers].kind``; its factory connects lazily, inside the tick, so a
    missing key fails that tick (recorded as ``error``), nothing else."""
    factory: BrokerFactory | None = None
    if settings.brokers.kind != "simulated":

        def factory(portfolio: Portfolio) -> Broker:
            return make_broker(settings, portfolio)

    return TickRuntime(
        settings=build_tick_settings(settings, universe),
        notifier=notifier_from_settings(settings),
        broker_factory=factory,
        books_from_subscriptions=settings.production.books_from_subscriptions,
    )

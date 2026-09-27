"""The one place that turns ``Settings`` into what a production tick runs
with, so every entrypoint (``stonks tick``, the REST API, ...) trades under
the same risk policy, shadow switch, alert routing and broker."""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import date

from stonks.accounts.models import DEFAULT_PORTFOLIO_ID
from stonks.accounts.models import Portfolio as AccountPortfolio
from stonks.config import Settings
from stonks.core.protocols import Broker
from stonks.core.types import Portfolio
from stonks.execution.brokers import SimulatedCosts, make_broker
from stonks.lab.parallel import default_max_workers
from stonks.notify import Notifier, notifier_from_settings
from stonks.production.tick import (
    BrokerFactory,
    TickPlan,
    TickSettings,
    TraderFactory,
    load_tick_plan,
)
from stonks.store.state import SqliteState


@dataclass(frozen=True)
class TickRuntime:
    settings: TickSettings
    notifier: Notifier
    #: None for the default simulated broker (the tick builds it itself).
    broker_factory: BrokerFactory | None = None
    #: ``[production].books_from_subscriptions``.
    books_from_subscriptions: bool = True

    def plan_for(self, state: SqliteState, *, dry_run: bool = False) -> TickPlan | None:
        """The books to trade: one per portfolio from its subscriptions when
        ``books_from_subscriptions`` is on, else ``None`` (the tick's
        default single book over every active strategy). A dry run's plan
        writes nothing."""
        if not self.books_from_subscriptions:
            return None
        return load_tick_plan(
            state, self.settings, traders=connection_traders(state), dry_run=dry_run
        )


def connection_traders(state: SqliteState) -> TraderFactory:
    """Auto books trade through their portfolio's connection
    (``ConnectionService.open_trader`` as ``service:scheduler``). The
    connections config and the master key load on first use, inside the
    tick, so a broken connection fails (and pauses) only its own book."""

    def open_trader(account: AccountPortfolio) -> Broker:
        from stonks.accounts.scope import Scope
        from stonks.connections.service import ConnectionService
        from stonks.connections.settings import ConnectionsConfig

        service = ConnectionService(state, ConnectionsConfig.load())
        return service.open_trader(Scope.service("scheduler"), account.id)

    return open_trader


def submit_broker_opener(
    settings: Settings, state: SqliteState, traders: TraderFactory | None = None
) -> Callable[[str], Broker]:
    """The broker each portfolio's tickets go to (the ``live_submit`` job,
    roadmap 19.8): the default portfolio's ``[brokers].kind`` broker when it
    is external, else the portfolio's trading connection. A portfolio that
    trades simulated money has no tickets to send and is refused."""
    open_trader = traders or connection_traders(state)

    def open_broker(portfolio_id: str) -> Broker:
        if portfolio_id == DEFAULT_PORTFOLIO_ID and settings.brokers.kind != "simulated":
            return make_broker(settings, Portfolio(cash=0.0))
        rows = state.sql("SELECT * FROM portfolios WHERE id = ?", [portfolio_id])
        if not rows:
            raise ValueError(f"portfolio {portfolio_id!r} not found")
        account = AccountPortfolio.from_row(rows[0])
        if account.kind != "broker":
            raise ValueError(f"portfolio {portfolio_id!r} does not trade at a broker")
        return open_trader(account)

    return open_broker


def build_tick_settings(
    settings: Settings,
    universe: Sequence[str],
    *,
    scoped: bool = False,
    bars_due: Mapping[str, date] | None = None,
) -> TickSettings:
    """Simulated fill costs follow ``SimulatedCosts.from_settings``:
    ``[backtest.costs]`` when configured (legacy ``[production]``
    ``slippage_bps`` / ``fee_per_trade`` then ignored), else the legacy pair.
    ``scoped``: the universe was narrowed by the caller (explicit tickers),
    so holdings outside it are left alone (``TickSettings.scoped``).
    ``bars_due``: a scheduled tick's due session bars (``TickSettings.bars_due``)."""
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
        risk_monitor=p.risk_monitor,
        decay=p.decay,
        scoped=scoped,
        bars_due=dict(bars_due) if bars_due else None,
        scoring_workers=p.scoring_workers or default_max_workers(),
        parallel_min_estimates=p.parallel_min_estimates,
        universe_id=p.universe if isinstance(p.universe, str) and not scoped else None,
        live=p.live,
    )


def build_tick_runtime(
    settings: Settings,
    universe: Sequence[str],
    *,
    scoped: bool = False,
    bars_due: Mapping[str, date] | None = None,
) -> TickRuntime:
    """The simulated default gets no factory (the tick builds its in-memory
    broker, no keys needed). ``alpaca`` is strictly opt-in via
    ``[brokers].kind``; its factory connects lazily, inside the tick, so a
    missing key fails that tick (recorded as ``error``), nothing else."""
    factory: BrokerFactory | None = None
    if settings.brokers.kind != "simulated":

        def factory(portfolio: Portfolio) -> Broker:
            return make_broker(settings, portfolio)

    return TickRuntime(
        settings=build_tick_settings(settings, universe, scoped=scoped, bars_due=bars_due),
        notifier=notifier_from_settings(settings),
        broker_factory=factory,
        books_from_subscriptions=settings.production.books_from_subscriptions,
    )

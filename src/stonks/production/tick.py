"""Production tick — one-shot entrypoint invoked by an external scheduler.

Each invocation is a fresh process. State lives in SqliteState + DuckDBLake;
the tick is stateless across runs. A tick has three phases (BL-12, design
``docs/design/accounts-and-modes.md`` section 5):

1. **Signal phase.** Every active strategy is scored once over the universe
   (:class:`~stonks.production.ranker.Ranker`); shadow strategies are scored
   once too when a book or the model books need them. The instance that
   scored is the instance that decides (:class:`StrategyPool`), so per-day
   state from ``estimate_return`` reaches ``decide``.
2. **Portfolio phase.** For each book of the :class:`TickPlan` (by default
   one: the default portfolio ``pf_default`` over every active strategy,
   today's single book), the construction pipeline
   (:func:`stonks.portfolio.pipeline.build_orders`) turns that book's
   signals into orders: its constructor (``single_winner`` by default), its
   risk policy, then its broker. Orders, fills and snapshots carry the
   book's ``portfolio_id``; position attribution and other
   ``"portfolio"``-stage hooks run in the transaction that writes them.
   With several books, each one soft-fails alone.
3. **Model books and hooks.** The shadow strategies' model books advance
   (``production.shadow``), strictly after the real ledgers committed, then
   the ``"tick"``-stage hooks run (``production.hooks``).

Orders are idempotent via a deterministic ``client_id`` derived from
(as_of date, portfolio, strategy_id, ticker, side), not from the per-run
``tick_id``, which stays unique so every run gets its own ``tick_runs`` row.
The default portfolio keeps the pre-accounts format
``<as_of>:<strategy>:<ticker>:<side>`` so a same-day re-run across the
upgrade still recognises its orders; every other portfolio's ids read
``<as_of>:<portfolio_id>:<strategy>:<ticker>:<side>``. Re-running a crashed
tick for the same ``as_of`` therefore reproduces the same client_ids, and
any order already in ``orders`` (unless ``rejected`` / ``cancelled``) is
skipped instead of being submitted (and applied to the portfolio) again.

The broker is the simulated one unless ``TickSettings.broker_kind`` opts in
to an external broker (``alpaca``, built by ``broker_factory``) for the
default portfolio. Then the tick reconciles open orders before deciding,
takes the portfolio from the broker account, commits each order row as
``pending`` *before* submitting it (so a crash never leaves a submitted
order unrecorded) and books statuses and fills only through
``execution.reconcile``, from the broker's state looked up by client id.
Other broker portfolios wait for their connection's trading adapter (S3/S6)
and are skipped.

Corporate actions: before anything decides, splits and cash dividends whose
ex-date falls after the stored snapshot's ``as_of`` and on or before this
tick's ``as_of`` are applied to the stored portfolio (simulated broker; an
external broker's account already reflects them) and splits to orders
still working from earlier ticks, exactly once per event; see
``production.corporate_actions``. Valuing and sizing stay on raw quotes.

A database still on the pre-accounts schema (no ``portfolio_id`` columns)
runs the default book only and writes the pre-accounts columns.
"""

from __future__ import annotations

import json
import uuid
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field, replace
from datetime import UTC, date, datetime
from typing import Any, Literal, get_args

from stonks.accounts.book import BookSpec
from stonks.accounts.models import DEFAULT_PORTFOLIO_ID, Mode, Subscription
from stonks.accounts.models import Portfolio as AccountPortfolio
from stonks.backtest.costs import CostModelSettings
from stonks.backtest.simulated_broker import SimulatedBroker
from stonks.core.protocols import Broker
from stonks.core.types import Fill, Order, OrderSide, OrderStatus, Portfolio
from stonks.execution.brokers.base import BrokerKind, OrderRejectedError, OrderStateSource
from stonks.execution.brokers.simulated import SimulatedCosts
from stonks.execution.orders import make_client_id
from stonks.execution.reconcile import (
    NON_TERMINAL_STATUSES,
    reconcile_order,
    reconcile_orders,
)
from stonks.logging import get_logger
from stonks.notify import Notification, Notifier
from stonks.portfolio.pipeline import (
    PORTFOLIO_STRATEGY,
    BookInput,
    ConstructionSettings,
    FillCosts,
    MarketView,
    apply_book_risk,
    build_orders,
    vols_from_history,
)
from stonks.production.corporate_actions import (
    adjust_working_orders,
    apply_corporate_actions,
    load_corporate_actions,
    record_as_dict,
    working_orders,
)
from stonks.production.hooks import (
    GateContext,
    NotifySignal,
    PortfolioHookContext,
    TickHookContext,
    run_gates,
    run_portfolio_hooks,
    run_tick_hooks,
)
from stonks.production.hooks.attribution import load_attribution
from stonks.production.ledger import ledger_filter
from stonks.production.prices import held_tickers, load_history, load_prices
from stonks.production.quit_rule import QuitRuleSettings
from stonks.production.ranker import Ranker, SignalSet, StrategyPool
from stonks.production.risk import RiskPolicy, build_risk_context, needs_risk_context
from stonks.production.shadow import evaluate_shadow_strategies, shadow_held_tickers
from stonks.registry.store import StrategyRegistry
from stonks.store.lake import DuckDBLake
from stonks.store.state import SqliteState

TickStatus = Literal["ok", "partial", "error", "noop"]
_DBTickStatus = Literal["running", "ok", "partial", "error"]

_log = get_logger("stonks.production.tick")

#: Builds the tick's broker around the portfolio it trades (a simulated
#: broker trades that object in memory; an external one ignores it).
BrokerFactory = Callable[[Portfolio], Broker]

#: Bars of daily history the volatility-aware constructors read.
_VOL_HISTORY_BARS = 260


class BackdatedTickError(ValueError):
    """A non-dry-run tick was asked to trade a date earlier than the latest
    portfolio snapshot. Trading it would apply today's portfolio to an old
    date and write a new "latest" snapshot that belongs in the past."""


@dataclass(frozen=True)
class TickSettings:
    universe: Sequence[str]
    threshold: float = 0.0
    initial_cash: float = 10_000.0
    #: Legacy flat costs of the simulated broker and the risk layer's cash
    #: estimate. Only used when ``costs`` is None.
    slippage_bps: float = 0.0
    fee_per_trade: float = 0.0
    #: The simulated broker's cost model (``[backtest.costs]``, the one
    #: backtests use); the risk layer's cash estimate uses it too. Exclusive with ``slippage_bps`` / ``fee_per_trade``:
    #: ``build_tick_settings`` resolves the precedence.
    costs: CostModelSettings | None = None
    # Closes older than this many calendar days before ``as_of`` are ignored,
    # so delisted / failed-ingest tickers never fill at months-old prices.
    max_price_staleness_days: int = 7
    risk: RiskPolicy = field(default_factory=RiskPolicy)
    # Evaluate shadow strategies against virtual portfolios (never traded).
    shadow_enabled: bool = True
    # "simulated" (default) trades in memory against the snapshot portfolio.
    # "alpaca" trades through ``run_tick``'s ``broker_factory``; the broker
    # account is then the source of truth for the portfolio.
    broker_kind: BrokerKind = "simulated"
    #: Fraction of each cash dividend withheld as tax (0 = credit in full).
    dividend_withholding_rate: float = 0.0
    #: ``[production.construction]``: the global constructor and no-trade
    #: buffer; a portfolio's ``construction_json`` is merged on top.
    construction: ConstructionSettings = field(default_factory=ConstructionSettings)
    #: Which strategies keep a model book: ``"shadow"`` (today) or ``"all"``
    #: non-retired strategies (design section 5; go-live then reads them).
    model_books: Literal["shadow", "all"] = "shadow"
    #: ``[production.quit_rule]``: read by the ``quit_rule`` tick hook.
    quit_rule: QuitRuleSettings = field(default_factory=QuitRuleSettings)

    def __post_init__(self) -> None:
        self.simulated_costs  # noqa: B018 - validates costs vs legacy (not both)
        if not 0.0 <= self.dividend_withholding_rate <= 1.0:
            raise ValueError(
                f"dividend_withholding_rate must be in [0, 1], got {self.dividend_withholding_rate}"
            )

    @property
    def simulated_costs(self) -> SimulatedCosts:
        return SimulatedCosts(
            model=self.costs, slippage_bps=self.slippage_bps, fee_per_trade=self.fee_per_trade
        )

    @property
    def fill_costs(self) -> FillCosts:
        return FillCosts(
            slippage_bps=self.slippage_bps, fee_per_trade=self.fee_per_trade, model=self.costs
        )


@dataclass(frozen=True)
class TickBook:
    """One book the tick trades.

    ``legacy``: the single book of an install whose subscriptions aren't
    wired into the tick yet: every active strategy, equal weight, the global
    risk policy and constructor (``spec.strategy_weights`` is ``None``).
    Otherwise the book's strategies are its subscriptions."""

    spec: BookSpec
    owner_id: str | None = None
    #: Subscription id per strategy (for attribution).
    subscription_ids: Mapping[str, str] = field(default_factory=dict)
    legacy: bool = False

    @property
    def portfolio_id(self) -> str:
        return self.spec.portfolio_id


@dataclass(frozen=True)
class TickPlan:
    """The books one tick trades and the notify-mode subscriptions it
    records signals for."""

    books: tuple[TickBook, ...]
    notify: tuple[Subscription, ...] = ()

    @classmethod
    def default(cls, settings: TickSettings) -> TickPlan:
        """Today's single book: ``pf_default`` over every active strategy."""
        spec = BookSpec(
            portfolio_id=DEFAULT_PORTFOLIO_ID,
            risk=settings.risk,
            broker="connection" if settings.broker_kind != "simulated" else "simulated",
            initial_cash=float(settings.initial_cash),
        )
        return cls(books=(TickBook(spec=spec, legacy=True),))


@dataclass(frozen=True)
class BookResult:
    portfolio_id: str
    status: TickStatus
    winner_strategy_id: str | None
    orders_placed: int
    fills: int
    summary: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class TickResult:
    tick_id: str
    status: TickStatus
    winner_strategy_id: str | None
    orders_placed: int
    fills: int
    portfolios: tuple[BookResult, ...] = ()


def run_tick(
    state: SqliteState,
    lake: DuckDBLake,
    registry: StrategyRegistry,
    settings: TickSettings,
    as_of: date | None = None,
    dry_run: bool = False,
    notifier: Notifier | None = None,
    broker_factory: BrokerFactory | None = None,
    plan: TickPlan | None = None,
) -> TickResult:
    """``plan``: the books to trade (default :meth:`TickPlan.default`; build
    one from portfolios and subscriptions with :func:`load_tick_plan`)."""
    as_of = as_of or utc_today()
    tick_id = _new_tick_id(as_of)
    started = _iso_now()
    log = _log.bind(tick_id=tick_id, as_of=as_of.isoformat(), dry_run=dry_run)
    plan = plan or TickPlan.default(settings)
    if not dry_run:
        _refuse_backdated(state, as_of, log, [b.portfolio_id for b in plan.books])

    state.execute(
        "INSERT INTO tick_runs (id, started_at, status) VALUES (?, ?, 'running')",
        [tick_id, started],
    )

    # Any failure past this point closes the tick as 'error' so the ledger
    # never keeps a row stuck at 'running'; the exception still propagates.
    try:
        result = _run_tick_body(
            state,
            lake,
            registry,
            settings,
            as_of,
            dry_run,
            tick_id=tick_id,
            log=log,
            notifier=notifier,
            broker_factory=broker_factory,
            plan=plan,
        )
    except Exception as exc:
        log.error("tick.error", error=str(exc), error_type=type(exc).__name__)
        try:
            _close_tick(
                state,
                tick_id,
                status="error",
                summary={"error": str(exc), "error_type": type(exc).__name__},
            )
        except Exception as close_exc:  # pragma: no cover - best effort
            log.error("tick.close_failed", error=str(close_exc))
        _safe_notify(
            notifier,
            Notification(
                level="error",
                title="tick failed",
                message=f"{type(exc).__name__}: {exc}",
                fields={"tick_id": tick_id, "as_of": as_of.isoformat(), "status": "error"},
            ),
            log,
        )
        raise
    if result.status == "partial":
        _safe_notify(
            notifier,
            Notification(
                level="warning",
                title="tick partially failed",
                message="one or more orders raised at the broker; see logs",
                fields={
                    "tick_id": tick_id,
                    "as_of": as_of.isoformat(),
                    "status": result.status,
                },
            ),
            log,
        )
    return result


# ---- the tick ----------------------------------------------------------------


@dataclass
class _TickRun:
    """What every book of one tick shares."""

    state: SqliteState
    lake: DuckDBLake
    registry: StrategyRegistry
    settings: TickSettings
    as_of: date
    dry_run: bool
    tick_id: str
    log: Any
    notifier: Notifier | None
    broker_factory: BrokerFactory | None
    plan: TickPlan
    #: The ledger tables carry ``portfolio_id`` (accounts schema).
    scoped: bool
    signals: SignalSet
    pool: StrategyPool
    _shadow: SignalSet | None = None
    _shadow_error: Exception | None = None
    _vols: dict[str, float] | None = None

    def shadow_signals(self) -> SignalSet:
        """Shadow strategies scored once per tick (raises what the scoring
        raised, every time it is asked)."""
        if self._shadow is None and self._shadow_error is None:
            try:
                self._shadow = Ranker(
                    registry=self.registry,
                    lake=self.lake,
                    universe=self.settings.universe,
                    threshold=self.settings.threshold,
                    status="shadow",
                ).score(as_of=self.as_of)
            except Exception as exc:
                self._shadow_error = exc
            else:
                self.pool.add(self._shadow)
        if self._shadow_error is not None:
            raise self._shadow_error
        assert self._shadow is not None
        return self._shadow

    def all_signals(self) -> SignalSet:
        """Active signals plus shadow ones (when they could be scored)."""
        try:
            return self.signals.merged(self.shadow_signals())
        except Exception as exc:
            self.log.error("tick.shadow_signals_failed", error=str(exc))
            return self.signals

    def vols(self, tickers: Sequence[str]) -> dict[str, float]:
        """Annual volatilities from daily history up to ``as_of``."""
        if self._vols is None:
            universe = sorted({*self.settings.universe, *tickers})
            history = load_history(self.lake, universe, self.as_of, bars=_VOL_HISTORY_BARS)
            self._vols = vols_from_history(history)
        missing = [t for t in tickers if t not in self._vols]
        if missing:
            history = load_history(self.lake, missing, self.as_of, bars=_VOL_HISTORY_BARS)
            self._vols.update(vols_from_history(history))
        return self._vols


def _run_tick_body(
    state: SqliteState,
    lake: DuckDBLake,
    registry: StrategyRegistry,
    settings: TickSettings,
    as_of: date,
    dry_run: bool,
    *,
    tick_id: str,
    log: Any,
    notifier: Notifier | None,
    broker_factory: BrokerFactory | None,
    plan: TickPlan,
) -> TickResult:
    scoped = _ledger_scoped(state)
    if not scoped and any(not b.legacy for b in plan.books):
        raise ValueError("portfolio books need the accounts schema; run `stonks db init`")

    # 1. signal phase: every active strategy scored once.
    ranker = Ranker(
        registry=registry,
        lake=lake,
        universe=settings.universe,
        threshold=settings.threshold,
    )
    signals = ranker.score(as_of=as_of)
    pool = StrategyPool(registry, lake)
    pool.add(signals)
    run = _TickRun(
        state=state,
        lake=lake,
        registry=registry,
        settings=settings,
        as_of=as_of,
        dry_run=dry_run,
        tick_id=tick_id,
        log=log,
        notifier=notifier,
        broker_factory=broker_factory,
        plan=plan,
        scoped=scoped,
        signals=signals,
        pool=pool,
    )
    _expect_consumers(run)

    # 2. portfolio phase: one book at a time, each in its own transactions.
    results: list[BookResult] = []
    single = len(plan.books) == 1
    for book in plan.books:
        try:
            results.append(_run_book(run, book))
        except Exception as exc:
            if single:
                raise
            log.error(
                "tick.portfolio_failed",
                portfolio_id=book.portfolio_id,
                error=str(exc),
                error_type=type(exc).__name__,
            )
            results.append(
                BookResult(
                    portfolio_id=book.portfolio_id,
                    status="error",
                    winner_strategy_id=None,
                    orders_placed=0,
                    fills=0,
                    summary={"error": str(exc), "error_type": type(exc).__name__},
                )
            )

    # 3. model books, strictly after the real ledgers committed, then hooks.
    shadow_summary = _shadow_phase(run)
    notify_signals = _notify_signals(run)
    hook_summary = run_tick_hooks(
        TickHookContext(
            state=state,
            lake=lake,
            tick_id=tick_id,
            as_of=as_of,
            dry_run=dry_run,
            signals=signals,
            portfolios={r.portfolio_id: r.summary for r in results},
            notify_signals=notify_signals,
            settings=settings,
            registry=registry,
        ),
        log,
    )

    status = _tick_status(results)
    if single:
        [only] = results
        summary = dict(only.summary)
        winner = only.winner_strategy_id
    else:
        summary = {
            "portfolios": {r.portfolio_id: {"status": r.status, **r.summary} for r in results},
            "orders_placed": sum(r.orders_placed for r in results),
            "fills": sum(r.fills for r in results),
        }
        winner = None
    summary.update(shadow_summary)
    summary.update(hook_summary)
    _close_tick(state, tick_id, status=status, summary=summary)
    if status == "noop":
        log.info("tick.noop", reason=summary.get("reason"))
    return TickResult(
        tick_id=tick_id,
        status=status,
        winner_strategy_id=winner,
        orders_placed=sum(r.orders_placed for r in results),
        fills=sum(r.fills for r in results),
        portfolios=tuple(results),
    )


def _tick_status(results: Sequence[BookResult]) -> TickStatus:
    if len(results) == 1:
        return results[0].status
    if any(r.status in ("error", "partial") for r in results):
        return "partial"
    if all(r.status == "noop" for r in results):  # also: no book to trade
        return "noop"
    return "ok"


def trades_live(book: TickBook, settings: TickSettings) -> bool:
    """The book trades at an external broker (today only the default
    portfolio, through ``[brokers].kind``)."""
    return book.portfolio_id == DEFAULT_PORTFOLIO_ID and settings.broker_kind != "simulated"


def book_strategies(book: TickBook, scored: Sequence[str], settings: TickSettings) -> list[str]:
    """The strategies whose signals a book trades. The legacy book trades
    every scored (active) strategy. A subscription book trades its paper and
    auto subscriptions, except at a live broker, where only auto ones place
    orders: paper money must never reach a real account."""
    if book.legacy or book.spec.strategy_weights is None:
        return list(scored)
    live = trades_live(book, settings)
    return [
        s
        for s, w in book.spec.strategy_weights.items()
        if w > 0 and (not live or book.spec.strategy_modes.get(s) is Mode.AUTO)
    ]


def _book_strategies(run: _TickRun, book: TickBook) -> list[str]:
    return book_strategies(book, list(run.signals.scores), run.settings)


def _needs_shadow_signals(run: _TickRun) -> bool:
    wanted = {s for b in run.plan.books if not b.legacy for s in _book_strategies(run, b)}
    wanted |= {s.strategy_id for s in run.plan.notify}
    return bool(wanted - set(run.signals.scores))


def _expect_consumers(run: _TickRun) -> None:
    """Declare how many consumers will decide with each strategy, so the
    pool copies a strategy before its first decision when others follow."""
    for book in run.plan.books:
        for sid in _book_strategies(run, book):
            run.pool.expect(sid)
    if run.dry_run or not run.settings.shadow_enabled:
        return
    for handle in run.registry.list_all(status="shadow"):
        run.pool.expect(handle.id)
    if run.settings.model_books == "all":
        for sid in run.signals.scores:
            run.pool.expect(sid)


def _run_book(run: _TickRun, book: TickBook) -> BookResult:
    state, lake, settings, as_of, log = run.state, run.lake, run.settings, run.as_of, run.log
    tick_id, dry_run = run.tick_id, run.dry_run
    portfolio_id = book.portfolio_id
    scope = portfolio_id if run.scoped else None
    log = log.bind(portfolio_id=portfolio_id) if not book.legacy else log
    universe = (
        list(book.spec.universe) if book.spec.universe is not None else list(settings.universe)
    )
    is_default = portfolio_id == DEFAULT_PORTFOLIO_ID

    # 2. prepare the portfolio, then price the universe plus every holding:
    #    a held ticker outside the universe must still be marked and sellable.
    #    An external broker (opt-in, e.g. Alpaca) is the source of truth: sync
    #    its order statuses and fills into the ledger first, then read the
    #    portfolio from the account.
    external = trades_live(book, settings)
    if book.spec.broker == "connection" and not external:
        log.warning("tick.portfolio_skipped", reason="no_connection_broker")
        return BookResult(
            portfolio_id=portfolio_id,
            status="noop",
            winner_strategy_id=None,
            orders_placed=0,
            fills=0,
            summary={"reason": "no_connection_broker"},
        )
    factory = run.broker_factory if is_default else None
    broker: Broker | None = None
    if external:
        if factory is None:
            raise ValueError(
                f"broker_kind={settings.broker_kind!r} needs a broker_factory "
                "(build it with production.settings_builder.build_tick_runtime)"
            )
        broker = factory(Portfolio(cash=0.0))
        if not isinstance(broker, OrderStateSource):
            raise TypeError(
                f"broker_kind={settings.broker_kind!r}: the broker must look orders up by "
                "client id (OrderStateSource) so crashed submissions can be reconciled"
            )
        if not dry_run:
            pre = reconcile_orders(broker, state, portfolio_id=portfolio_id)
            log.info(
                "tick.reconciled",
                orders_checked=pre.orders_checked,
                fills_inserted=pre.fills_inserted,
            )
        portfolio = broker.fetch_portfolio()
    else:
        portfolio = _load_or_seed_portfolio(
            state, initial_cash=book.spec.initial_cash, portfolio_id=scope
        )

    # 2b. corporate actions since the stored snapshot, before anything sizes.
    #     Derived from the snapshot's as_of, so persisted exactly when this
    #     tick's snapshot is (see production.corporate_actions).
    since = _latest_snapshot_as_of(state, portfolio_id=scope)
    working = _working_orders(state, scope)
    actions = load_corporate_actions(lake, [*held_tickers(portfolio.positions), *working.values()])
    applied = []
    if not external:
        applied = apply_corporate_actions(
            portfolio,
            actions,
            since=since,
            as_of=as_of,
            withholding_rate=settings.dividend_withholding_rate,
        )
        for record in applied:
            log.info("tick.corporate_action", **record_as_dict(record))
    corporate_summary: dict[str, Any] = (
        {"corporate_actions": [record_as_dict(r) for r in applied]} if applied else {}
    )

    def persist_corporate_actions() -> int:
        """Split-adjust the working orders; call inside the transaction
        that writes this tick's snapshot."""
        return adjust_working_orders(
            state, list(working), actions, since=since, as_of=as_of, now=_iso_now()
        )

    held = held_tickers(portfolio.positions)
    book_prices = load_prices(
        lake,
        universe,
        held,
        as_of,
        max_staleness_days=settings.max_price_staleness_days,
    )
    prices = book_prices.prices

    def result(status: TickStatus, winner: str | None, placed: int, fills: int, summary: dict):
        return BookResult(
            portfolio_id=portfolio_id,
            status=status,
            winner_strategy_id=winner,
            orders_placed=placed,
            fills=fills,
            summary=summary,
        )

    def noop(reason: str) -> BookResult:
        if not dry_run:
            # nothing trades, but applied events must still be persisted
            with state.transaction():
                if persist_corporate_actions() or applied:
                    _snapshot_portfolio(
                        state, tick_id, portfolio, prices, as_of, portfolio_id=scope
                    )
        log.info("tick.portfolio_noop", reason=reason)
        return result("noop", None, 0, 0, {"reason": reason, **corporate_summary})

    # 3. construct: the pipeline turns this book's signals into orders
    #    (decide or targets, stale buys dropped, then the risk layer).
    strategy_ids = _book_strategies(run, book)
    signal_set = run.all_signals() if _needs_shadow_signals(run) else run.signals
    signals = {
        sid: _in_universe(signal_set.scores[sid], book.spec.universe)
        for sid in signal_set.scores
        if sid in strategy_ids
    }
    construction = (
        settings.construction
        if book.legacy
        else ConstructionSettings.from_mapping(book.spec.construction)
    )
    asset_classes = _asset_classes(lake, [*universe, *portfolio.positions])
    market = MarketView(
        as_of=as_of,
        prices=prices,
        buyable=book_prices.fresh,
        volumes=book_prices.volumes,
        asset_classes=asset_classes,
        vols_annual=({} if construction.is_single_winner else run.vols([*universe, *held])),
    )
    # Rules that need history (W3.1) run only with a context: built when the
    # book's policy (or a strategy slice's) enables one.
    risk_context = None
    if needs_risk_context(book.spec.risk, *book.spec.risk_overrides.values()):
        risk_context = build_risk_context(
            lake,
            state,
            portfolio,
            prices,
            as_of,
            policy=book.spec.risk,
            universe=universe,
            cost_model=settings.costs,
            volumes=book_prices.volumes,
            portfolio_id=portfolio_id,
        )
    book_input = BookInput(
        portfolio=portfolio,
        construction=construction,
        risk=book.spec.risk,
        strategy_weights=None if book.legacy else book.spec.strategy_weights,
        risk_overrides=book.spec.risk_overrides,
        prior_attribution=(load_attribution(state, portfolio_id, as_of) if run.scoped else {}),
        costs=settings.fill_costs,
        risk_context=risk_context,
    )
    candidates = None if book.legacy else set(strategy_ids)

    def exit_owner() -> str | None:
        owner = _position_owner(
            state, run.registry, held, log, portfolio_id=scope, candidates=candidates
        )
        if owner is not None:
            log.info("tick.exit_decision", strategy_id=owner, held=held)
        return owner

    make_id = _client_id_fn(as_of, portfolio_id)
    pipeline = build_orders(
        signals,
        book_input,
        market,
        strategies=run.pool.checkout,
        exit_owner=exit_owner,
        client_id=make_id,
    )
    if pipeline.reason is not None:
        return noop(pipeline.reason)
    winner_id = pipeline.decided_by
    if winner_id is not None and not pipeline.exit_only:
        log.info(
            "tick.winner",
            strategy_id=winner_id,
            expected_return=pipeline.winner_return,
        )
    risk_adjustments = list(pipeline.adjustments)
    slice_policy = book_input.risk_overrides.get(winner_id) if winner_id else None

    halt = run_gates(
        GateContext(
            state=state,
            as_of=as_of,
            portfolio_id=portfolio_id,
            owner_id=book.owner_id,
            dry_run=dry_run,
            policy=book.spec.risk,
        ),
        log,
    )
    proposed = pipeline.orders
    if halt is not None:
        log.warning("tick.portfolio_halted", halt=halt.halt, gate=halt.gate, reason=halt.reason)
        proposed = [] if halt.halt == "all" else [o for o in proposed if o.side == "sell"]

    if broker is None:
        broker = _build_broker(
            portfolio, settings, prices, as_of, factory, book_prices.volumes, asset_classes
        )
    orders_with_tick = [replace(o, tick_id=tick_id) for o in proposed]
    open_conflicts: list[dict[str, str]] = []
    if external:
        orders_with_tick, open_conflicts = _drop_open_order_conflicts(
            state, orders_with_tick, log, portfolio_id=scope
        )
    sells = [o for o in orders_with_tick if o.side == "sell"]
    buys = [o for o in orders_with_tick if o.side == "buy"]

    placed = 0
    fills_count = 0
    any_failure = False
    # Simulated: broker outcomes are buffered and persisted together with the
    # portfolio snapshot in one transaction, so a crash can't leave fills
    # recorded without the snapshot that reflects them. External: see
    # ``place_external``.
    outcomes: list[tuple[Order, OrderStatus, Fill | None]] = []

    def place_external(order: Order) -> None:
        """The order row is committed as 'pending' *before* the broker sees
        the order, so no crash can leave a submitted order unrecorded: the
        next tick's ``reconcile_orders`` looks every pending row up by
        client id and books it, or rejects it when the broker never received
        it. Right after a submit the row is synced (broker id, status, fill)
        through the same ``reconcile_order``; fills are only ever booked from
        the broker's cumulative state, so never twice."""
        nonlocal placed, fills_count, any_failure
        with state.transaction():
            _record_order(state, order, status="pending", portfolio_id=scope)
        try:
            broker.place_order(order)
        except OrderRejectedError as exc:
            log.warning("tick.order.rejected", ticker=order.ticker, error=str(exc))
            _record_order(state, order, status="rejected", reason=str(exc), portfolio_id=scope)
            outcomes.append((order, "rejected", None))
            return
        except Exception as exc:
            # It may or may not have reached the broker: the row stays
            # pending and the next tick's reconcile settles it by client id.
            any_failure = True
            log.warning("tick.order.failed", ticker=order.ticker, error=str(exc))
            return
        placed += 1
        outcomes.append((order, "pending", None))
        try:
            synced = reconcile_order(broker, state, order.client_id, reject_unknown=False)
        except Exception as exc:
            log.warning("tick.order.sync_failed", client_id=order.client_id, error=str(exc))
            return
        fills_count += int(synced.fill_inserted)

    def place(batch: list[Order]) -> None:
        nonlocal placed, fills_count, any_failure
        for order in batch:
            if dry_run:
                placed += 1
                continue
            existing = _live_order_status(state, order.client_id)
            if existing is not None:
                log.info(
                    "tick.order.skipped_already_submitted",
                    client_id=order.client_id,
                    status=existing,
                )
                continue
            if external:
                place_external(order)
                continue
            try:
                fill = broker.place_order(order)
            except OrderRejectedError as exc:
                log.warning("tick.order.rejected", ticker=order.ticker, error=str(exc))
                outcomes.append((order, "rejected", None))
                continue
            except Exception as exc:
                any_failure = True
                log.warning("tick.order.failed", ticker=order.ticker, error=str(exc))
                continue
            placed += 1
            outcomes.append((order, "filled" if fill else "rejected", fill))
            if fill is not None:
                fills_count += 1

    # Sells first. The first risk pass counted their expected proceeds, so
    # buys are re-checked against the portfolio as it stands after the
    # sells: a rejected or unfilled sell must not fund a buy. (An external
    # broker's portfolio object is not updated by fills, so its buys are
    # checked against the pre-sell cash: conservative by construction.)
    place(sells)
    if buys and not dry_run:
        second = apply_book_risk(buys, book_input, market, slice_policy)
        risk_adjustments.extend(second.adjustments)
        buys = second.orders
    place(buys)

    def hooks(after: Portfolio, marks: Mapping[str, float]) -> dict[str, Any]:
        return run_portfolio_hooks(
            PortfolioHookContext(
                state=state,
                tick_id=tick_id,
                as_of=as_of,
                portfolio_id=portfolio_id,
                portfolio=after,
                prices=marks,
                pipeline=pipeline,
                outcomes=outcomes,
                subscription_ids=book.subscription_ids,
                scoped=run.scoped,
            ),
            log,
        )

    hook_summary: dict[str, Any] = {}
    if not dry_run and external:
        # Report what the broker made of each submission (e.g. rejected).
        booked = _order_statuses(state, [o.client_id for o, _, _ in outcomes])
        outcomes = [(o, booked.get(o.client_id, st), f) for o, st, f in outcomes]
        after = broker.fetch_portfolio()
        marks = dict(prices)
        unpriced = [t for t in held_tickers(after.positions) if t not in marks]
        if unpriced:
            marks.update(
                load_prices(
                    lake,
                    [],
                    unpriced,
                    as_of,
                    max_staleness_days=settings.max_price_staleness_days,
                ).prices
            )
        with state.transaction():
            persist_corporate_actions()
            _snapshot_portfolio(state, tick_id, after, marks, as_of, portfolio_id=scope)
            hook_summary = hooks(after, marks)
    elif not dry_run:
        with state.transaction():
            for order, order_status, fill in outcomes:
                _record_order(state, order, status=order_status, portfolio_id=scope)
                if fill is not None:
                    _record_fill(state, fill, portfolio_id=scope)
            persist_corporate_actions()
            _snapshot_portfolio(state, tick_id, portfolio, prices, as_of, portfolio_id=scope)
            hook_summary = hooks(portfolio, prices)

    rejected = [order.ticker for order, st, _ in outcomes if st == "rejected"]
    if rejected:
        fields: dict[str, Any] = {
            "tick_id": tick_id,
            "as_of": as_of.isoformat(),
            "rejected": rejected,
        }
        if not book.legacy:
            fields["portfolio_id"] = portfolio_id
        _safe_notify(
            run.notifier,
            Notification(
                level="warning",
                title="orders rejected",
                message=f"{len(rejected)} order(s) rejected by the broker",
                fields=fields,
            ),
            log,
        )

    exit_strategy_id = winner_id if pipeline.exit_only else None
    status: TickStatus = "ok" if not any_failure else "partial"
    return result(
        status,
        None if exit_strategy_id else winner_id,
        placed,
        fills_count,
        {
            "winner_strategy_id": None if exit_strategy_id else winner_id,
            "winner_expected_return": pipeline.winner_return,
            **(
                {"reason": "no_candidates", "exit_strategy_id": exit_strategy_id}
                if exit_strategy_id
                else {}
            ),
            "stale_buys_dropped": pipeline.stale_buys,
            **({"open_order_conflicts": open_conflicts} if open_conflicts else {}),
            **(
                {"halted": {"halt": halt.halt, "gate": halt.gate, "reason": halt.reason}}
                if halt is not None
                else {}
            ),
            **({"constructor": construction.method} if not book.legacy else {}),
            "orders_placed": placed,
            "fills": fills_count,
            "risk_adjustments": [a.as_dict() for a in risk_adjustments],
            **corporate_summary,
            **hook_summary,
        },
    )


# ---- plans from portfolios and subscriptions -----------------------------------------


def load_tick_plan(state: SqliteState, settings: TickSettings) -> TickPlan:
    """One book per active portfolio (of an active owner) with at least one
    enabled paper or auto subscription, plus every enabled notify-mode
    subscription. Auto subscriptions count only on portfolios that trade at
    a broker (the auto gate refuses others; this is the tick's own guard):
    ``kind = broker``, or the default portfolio of an install whose
    ``[brokers].kind`` is external (its connection row arrives with S3)."""
    live_default = settings.broker_kind != "simulated"
    rows = state.sql(
        "SELECT p.* FROM portfolios p JOIN users u ON u.id = p.owner_id"
        " WHERE p.status = 'active' AND u.status = 'active' ORDER BY p.created_at, p.id"
    )
    subs = [
        Subscription.from_row(r)
        for r in state.sql(
            "SELECT s.* FROM subscriptions s JOIN users u ON u.id = s.user_id"
            " WHERE s.enabled = 1 AND u.status = 'active' ORDER BY s.created_at, s.id"
        )
    ]
    global_construction = _construction_mapping(settings.construction)
    books: list[TickBook] = []
    for row in rows:
        portfolio = AccountPortfolio.from_row(row)
        own = [
            s
            for s in subs
            if s.portfolio_id == portfolio.id
            and (
                s.mode is not Mode.AUTO
                or portfolio.kind == "broker"
                or (live_default and portfolio.id == DEFAULT_PORTFOLIO_ID)
            )
        ]
        spec = BookSpec.for_portfolio(
            portfolio,
            own,
            global_risk=settings.risk,
            global_construction=global_construction,
            default_initial_cash=settings.initial_cash,
        )
        if not spec.strategy_weights:
            continue
        books.append(
            TickBook(
                spec=spec,
                owner_id=portfolio.owner_id,
                subscription_ids={
                    s.strategy_id: s.id for s in own if s.strategy_id in spec.strategy_weights
                },
            )
        )
    notify = tuple(s for s in subs if s.mode is Mode.NOTIFY)
    return TickPlan(books=tuple(books), notify=notify)


def _construction_mapping(construction: ConstructionSettings) -> dict[str, Any]:
    """The flat mapping :func:`stonks.accounts.book.merge_construction` merges."""
    flat = construction.model_dump(exclude={"params"})
    flat.update(construction.params)
    return {k: v for k, v in flat.items() if v is not None}


def _notify_signals(run: _TickRun) -> list[NotifySignal]:
    if not run.plan.notify:
        return []
    signals = run.all_signals() if _needs_shadow_signals(run) else run.signals
    out: list[NotifySignal] = []
    for sub in run.plan.notify:
        scores = signals.scores.get(sub.strategy_id)
        if not scores:
            continue
        picks = sorted(scores.items(), key=lambda p: p[1], reverse=True)
        out.append(
            NotifySignal(
                subscription_id=sub.id,
                user_id=sub.user_id,
                strategy_id=sub.strategy_id,
                portfolio_id=sub.portfolio_id,
                picks=tuple(picks),
            )
        )
    return out


# ---- helpers ---------------------------------------------------------------


def _client_id_fn(as_of: date, portfolio_id: str) -> Callable[[str | None, str, OrderSide], str]:
    """Deterministic client ids (:func:`make_client_id`: the default
    portfolio keeps the pre-accounts format, others carry their id)."""

    def make(strategy_id: str | None, ticker: str, side: OrderSide) -> str:
        return make_client_id(
            as_of=as_of,
            strategy_id=strategy_id or PORTFOLIO_STRATEGY,
            ticker=ticker,
            side=side,
            portfolio_id=portfolio_id,
        )

    return make


def _in_universe(scores: Mapping[str, float], universe: Sequence[str] | None) -> dict[str, float]:
    if universe is None:
        return dict(scores)
    allowed = set(universe)
    return {t: r for t, r in scores.items() if t in allowed}


def _build_broker(
    portfolio: Portfolio,
    settings: TickSettings,
    prices: dict[str, float],
    as_of: date,
    factory: BrokerFactory | None = None,
    volumes: dict[str, float] | None = None,
    asset_classes: dict[str, str] | None = None,
) -> Broker:
    """The one place the tick constructs its broker around ``portfolio``.

    A simulated broker fills at the latest close through
    ``settings.simulated_costs`` and gets what that cost model reads, as in
    backtests: each ticker's asset class and the volume of the priced bar."""
    if factory is not None:
        broker = factory(portfolio)
    else:
        broker = settings.simulated_costs.build_broker(portfolio)
    if isinstance(broker, SimulatedBroker):
        broker.set_asset_classes(asset_classes or {})  # type: ignore[arg-type]
        broker.set_prices(prices, as_of=as_of, volumes=volumes)
    return broker


def _shadow_phase(run: _TickRun) -> dict[str, Any]:
    """Advance the model books; return the tick-summary fragment. Never
    raises: a model-book failure must not fail the real tick."""
    settings, lake, state, log = run.settings, run.lake, run.state, run.log
    if run.dry_run or not settings.shadow_enabled:
        return {}
    try:
        shadow = run.shadow_signals()
        statuses: tuple[str, ...] = ("shadow",)
        if settings.model_books == "all":
            shadow = run.signals.merged(shadow)
            statuses = ("active", "shadow")
        # Shadow portfolios may hold tickers outside the universe too.
        shadow_held = shadow_held_tickers(state)
        shadow_actions = load_corporate_actions(lake, shadow_held)
        book = load_prices(
            lake,
            settings.universe,
            shadow_held,
            run.as_of,
            max_staleness_days=settings.max_price_staleness_days,
        )
        shadow_prices = book.prices
        base_context = None
        if needs_risk_context(settings.risk):
            base_context = build_risk_context(
                lake,
                state,
                Portfolio(cash=0.0),
                shadow_prices,
                run.as_of,
                policy=settings.risk,
                universe=[*settings.universe, *shadow_held],
                cost_model=settings.costs,
                volumes=book.volumes,
            )
        outcomes = evaluate_shadow_strategies(
            state,
            run.registry,
            shadow.ranked(),
            book.prices,
            _asset_classes(lake, [*settings.universe, *shadow_held]),
            run.as_of,
            run.tick_id,
            settings,
            buyable=book.fresh,
            volumes=book.volumes,
            corporate_actions=shadow_actions,
            strategies=run.pool.checkout,
            statuses=statuses,
            risk_context=base_context,
        )
    except Exception as exc:
        log.error("tick.shadow_failed", error=str(exc), error_type=type(exc).__name__)
        return {"shadow_error": f"{type(exc).__name__}: {exc}"}
    return {"shadow": [o.as_dict() for o in outcomes]}


def _safe_notify(notifier: Notifier | None, notification: Notification, log: Any) -> None:
    """Notifiers promise never to raise; guard anyway so a misbehaving one
    can never fail or mask the outcome of a tick."""
    if notifier is None:
        return
    try:
        notifier.notify(notification)
    except Exception as exc:
        log.error("tick.notify_failed", error=str(exc), error_type=type(exc).__name__)


def _asset_classes(lake: DuckDBLake, tickers: Sequence[str]) -> dict[str, str]:
    unique = sorted(set(tickers))
    return lake.get_asset_classes(unique) if unique else {}


def utc_today(clock: Callable[[], datetime] | None = None) -> date:
    """Today's date in UTC, the calendar all stored timestamps use.
    ``clock`` returns an aware datetime (default: the system clock)."""
    return (clock() if clock is not None else datetime.now(UTC)).astimezone(UTC).date()


def _new_tick_id(as_of: date) -> str:
    return f"tick_{as_of.isoformat()}_{uuid.uuid4().hex[:8]}"


def _iso_now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def _ledger_scoped(state: SqliteState) -> bool:
    """The ledger tables carry ``portfolio_id`` (accounts schema, 010)."""
    cols = {r["name"] for r in state.sql("SELECT name FROM pragma_table_info('orders')")}
    return "portfolio_id" in cols


def _scoped(portfolio_id: str | None, sql: str, params: list[Any]) -> tuple[str, list[Any]]:
    """``sql`` (ending in a WHERE clause or none) narrowed to one portfolio."""
    if portfolio_id is None:
        return sql, params
    joiner = " AND " if " WHERE " in sql else " WHERE "
    return f"{sql}{joiner}portfolio_id = ?", [*params, portfolio_id]


def _refuse_backdated(
    state: SqliteState, as_of: date, log: Any, portfolio_ids: Sequence[str]
) -> None:
    """Refuse when any traded portfolio's own ledger (tick snapshots; a
    broker sync's rows are not the tick's) is already past ``as_of``."""
    for portfolio_id in portfolio_ids:
        latest = _latest_snapshot_as_of(state, portfolio_id=portfolio_id)
        if latest is not None and as_of < latest:
            log.error(
                "tick.backdated_refused",
                latest_snapshot_as_of=latest.isoformat(),
                portfolio_id=portfolio_id,
            )
            raise BackdatedTickError(
                f"refusing to trade as_of {as_of.isoformat()}: the portfolio already has a "
                f"snapshot for {latest.isoformat()}; run with --dry-run to inspect a past date"
            )


def _latest_snapshot_as_of(state: SqliteState, portfolio_id: str | None = None) -> date | None:
    """``as_of`` of the portfolio's latest dated tick snapshot (``None``: none)."""
    where, params = ledger_filter(state, "portfolio_snapshots", portfolio_id, tick_only=True)
    latest = state.sql(
        f"SELECT MAX(as_of) AS as_of FROM portfolio_snapshots WHERE {where}", params
    )[0]["as_of"]
    return date.fromisoformat(latest) if latest else None


def _load_or_seed_portfolio(
    state: SqliteState, initial_cash: float, portfolio_id: str | None = None
) -> Portfolio:
    # NULL as_of (rows written without one) sorts last under DESC.
    where, params = ledger_filter(state, "portfolio_snapshots", portfolio_id, tick_only=True)
    rows = state.sql(
        f"SELECT cash, positions_json FROM portfolio_snapshots WHERE {where}"
        " ORDER BY as_of DESC, id DESC LIMIT 1",
        params,
    )
    if not rows:
        return Portfolio(cash=initial_cash, positions={})
    row = rows[0]
    return Portfolio(cash=float(row["cash"]), positions=json.loads(row["positions_json"]))


def _working_orders(state: SqliteState, portfolio_id: str | None) -> dict[str, str]:
    """Orders still working at the broker, of this portfolio only, so a
    split adjusts each portfolio's orders exactly once."""
    working = working_orders(state)
    if portfolio_id is None or not working:
        return working
    marks = ",".join("?" for _ in working)
    rows = state.sql(
        f"SELECT client_id FROM orders WHERE portfolio_id = ? AND client_id IN ({marks})",
        [portfolio_id, *working],
    )
    mine = {r["client_id"] for r in rows}
    return {cid: t for cid, t in working.items() if cid in mine}


def _position_owner(
    state: SqliteState,
    registry: StrategyRegistry,
    held: Sequence[str],
    log: Any,
    portfolio_id: str | None = None,
    candidates: set[str] | None = None,
) -> str | None:
    """The active strategy behind the most recent fill on a held ticker of
    this portfolio, or None when every strategy that bought the holdings is
    no longer active (or, for a subscription book, no longer in it)."""
    placeholders = ",".join("?" for _ in held)
    sql, params = _scoped(
        portfolio_id,
        "SELECT strategy_id FROM orders"
        " WHERE status IN ('filled', 'partially_filled') AND strategy_id IS NOT NULL"
        f" AND ticker IN ({placeholders})",
        list(held),
    )
    rows = state.sql(f"{sql} ORDER BY updated_at DESC, created_at DESC, rowid DESC", params)
    allowed = {h.id for h in registry.list_all(status="active")}
    if candidates is not None:
        shadow = {h.id for h in registry.list_all(status="shadow")}
        allowed = (allowed | shadow) & candidates
    for row in rows:
        if row["strategy_id"] in allowed:
            return row["strategy_id"]
    log.warning(
        "tick.exit.no_active_owner",
        held=list(held),
        owners=sorted({r["strategy_id"] for r in rows}),
    )
    return None


def _drop_open_order_conflicts(
    state: SqliteState, orders: list[Order], log: Any, portfolio_id: str | None = None
) -> tuple[list[Order], list[dict[str, str]]]:
    """Drop orders on a (ticker, side) that already has a working order at
    the broker under another client_id (e.g. yesterday's GTC buy still
    open): the portfolio doesn't show it yet, so deciding again would double
    the position once both fill."""
    placeholders = ",".join("?" for _ in NON_TERMINAL_STATUSES)
    sql, params = _scoped(
        portfolio_id,
        f"SELECT client_id, ticker, side FROM orders WHERE status IN ({placeholders})",
        list(NON_TERMINAL_STATUSES),
    )
    open_rows = state.sql(sql, params)
    kept: list[Order] = []
    conflicts: list[dict[str, str]] = []
    for order in orders:
        clash = [
            r["client_id"]
            for r in open_rows
            if r["ticker"] == order.ticker
            and r["side"] == order.side
            and r["client_id"] != order.client_id
        ]
        if clash:
            log.warning(
                "tick.order.open_order_conflict",
                ticker=order.ticker,
                side=order.side,
                open_client_ids=clash,
            )
            conflicts.append({"ticker": order.ticker, "side": order.side})
            continue
        kept.append(order)
    return kept, conflicts


_ORDER_STATUSES: frozenset[str] = frozenset(get_args(OrderStatus))


def _order_statuses(state: SqliteState, client_ids: Sequence[str | None]) -> dict[str, OrderStatus]:
    """Booked status per client id. Only known ``OrderStatus`` values are
    returned, so the caller falls back to the submission's own status."""
    ids = [c for c in client_ids if c]
    if not ids:
        return {}
    placeholders = ",".join("?" for _ in ids)
    rows = state.sql(
        f"SELECT client_id, status FROM orders WHERE client_id IN ({placeholders})", ids
    )
    return {r["client_id"]: r["status"] for r in rows if r["status"] in _ORDER_STATUSES}


def _live_order_status(state: SqliteState, client_id: str | None) -> str | None:
    """Status of an order already in the ledger that must not be placed
    again (anything but rejected/cancelled), or None when it may be placed."""
    rows = state.sql(
        "SELECT status FROM orders WHERE client_id = ? AND status NOT IN ('rejected', 'cancelled')",
        [client_id],
    )
    return rows[0]["status"] if rows else None


def _record_order(
    state: SqliteState,
    order: Order,
    status: OrderStatus,
    reason: str | None = None,
    portfolio_id: str | None = None,
) -> None:
    # ON CONFLICT DO UPDATE lets a retried tick progress the status of a
    # previously-``rejected`` order to ``filled`` if the re-submission
    # succeeds (clearing the old rejection reason). The WHERE guard avoids
    # rewriting ``updated_at`` when nothing changed (a retried tick
    # replaying an already-filled order). ``portfolio_id`` is written only
    # on insert (it is immutable).
    now = _iso_now()
    extra_col, extra_val = (", portfolio_id", ", ?") if portfolio_id is not None else ("", "")
    state.execute(
        f"""
        INSERT INTO orders
            (client_id, tick_id, strategy_id, ticker, side, quantity,
             order_type, limit_price, status, status_reason, broker_order_id,
             created_at, updated_at{extra_col})
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, NULL, ?, ?{extra_val})
        ON CONFLICT (client_id) DO UPDATE SET
            status = excluded.status,
            status_reason = excluded.status_reason,
            updated_at = excluded.updated_at
          WHERE orders.status IS NOT excluded.status
             OR orders.status_reason IS NOT excluded.status_reason
        """,
        [
            order.client_id,
            order.tick_id,
            order.strategy_id,
            order.ticker,
            order.side,
            order.quantity,
            order.order_type,
            order.limit_price,
            status,
            reason,
            now,
            now,
            *([portfolio_id] if portfolio_id is not None else []),
        ],
    )


def _record_fill(state: SqliteState, fill: Fill, portfolio_id: str | None = None) -> None:
    extra_col, extra_val = (", portfolio_id", ", ?") if portfolio_id is not None else ("", "")
    state.execute(
        f"""
        INSERT INTO fills
            (order_client_id, ticker, quantity, price, fee, filled_at{extra_col})
        VALUES (?, ?, ?, ?, ?, ?{extra_val})
        """,
        [
            fill.order_client_id,
            fill.ticker,
            fill.quantity,
            fill.price,
            fill.fee,
            fill.filled_at.isoformat(timespec="seconds"),
            *([portfolio_id] if portfolio_id is not None else []),
        ],
    )


def _snapshot_portfolio(
    state: SqliteState,
    tick_id: str,
    portfolio: Portfolio,
    prices: dict[str, float],
    as_of: date,
    portfolio_id: str | None = None,
) -> None:
    total = portfolio.total_value(prices)
    extra_col, extra_val = (", portfolio_id", ", ?") if portfolio_id is not None else ("", "")
    state.execute(
        f"""
        INSERT INTO portfolio_snapshots
            (tick_id, as_of, taken_at, cash, positions_json, total_value{extra_col})
        VALUES (?, ?, ?, ?, ?, ?{extra_val})
        """,
        [
            tick_id,
            as_of.isoformat(),
            _iso_now(),
            portfolio.cash,
            json.dumps(portfolio.positions, sort_keys=True),
            total,
            *([portfolio_id] if portfolio_id is not None else []),
        ],
    )


def _close_tick(state: SqliteState, tick_id: str, status: TickStatus, summary: dict) -> None:
    # The tick_runs CHECK constraint accepts 'running' | 'ok' | 'partial' | 'error'.
    # 'noop' is a TickResult-only distinction (no candidates were ranked); persist
    # it as 'ok' and leave the "it was a no-op" signal in summary_json.
    db_status: _DBTickStatus = "ok" if status == "noop" else status
    state.execute(
        "UPDATE tick_runs SET finished_at=?, status=?, summary_json=? WHERE id=?",
        [_iso_now(), db_status, json.dumps(summary, sort_keys=True), tick_id],
    )

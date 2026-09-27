"""The engine process: the always-on intraday loop (roadmap 21.2.5).

:class:`EngineProcess` wires the parts of 21.2 into one loop for the
intraday books:

```
live:   StreamRunner (reconnects, bars, backfill) --every event--> EngineProcess.on_event
replay: a finite StreamingSource (a recording, lake bars) -------> EngineProcess.on_event

EngineProcess.on_event -> EventDriver -> bar close -> EngineProcess.on_bar_close:
    1. skip a close the crashed run already handled (the checkpoint)
    2. write the decision bars to the bar store (live), so the step sees them
    3. per book: IntradayRouter.on_bar_close (simulated fills, reconcile)
    4. the step learns the new fills from the ledger
    5. DecisionStep: signals, build_orders, session gates, flatten orders
    6. per book: IntradayRouter.route(orders)
    7. checkpoint and heartbeat in engine_runs
```

- **Startup** (:meth:`EngineProcess.start`) runs before the first order.
  A run left ``running`` is marked ``crashed``. A simulated book is
  rebuilt from the ledger (starting cash plus fills), and its orders that
  died with the old process are ended. Then every router runs the startup
  reconciliation and ``require_reconciled``: an order still ``unknown``
  stops the start. Nothing is sent before this passes.
- **Restart without re-sending.** Client ids are deterministic and the
  router never sends one already in the ledger. A replay that resumes after
  a crash also skips every close up to the crashed run's checkpoint.
- **Flatten.** A book with ``flatten_at_close`` gets closing orders from
  the step in the flatten window, sent through the router like any other
  order. When the step fails on a bar, the flatten orders are still built
  and sent.
- **Stop.** A stop request in the control directory, :meth:`stop`, the end
  of a finite source or the ``stop_at`` time (the close plus a margin) ends
  the loop. Simulated day orders still working then expire, and every book
  is reconciled one last time.

:func:`build_engine` builds a process from ``[engine]``. The entry point is
``python -m stonks.engine run|replay``.
"""

from __future__ import annotations

import os
import threading
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field, replace
from datetime import UTC, date, datetime, timedelta
from typing import TYPE_CHECKING, Any

from stonks.core.clock import SYSTEM_CLOCK, Clock
from stonks.core.interval import Interval
from stonks.core.protocols import Broker, Strategy
from stonks.core.stream import StreamEvent
from stonks.core.types import Order
from stonks.engine.control import EngineControl
from stonks.engine.driver import BarClose, EventDriver
from stonks.engine.recovery import (
    EngineMode,
    EngineRun,
    EngineRuns,
    end_lost_orders,
    ledger_fills,
    ledger_portfolio,
)
from stonks.engine.router import ROUTER_PRIORITY, IntradayRouter, RouteAck
from stonks.engine.sessions import (
    NO_HALTS,
    HaltTable,
    SessionRulebook,
    flatten_orders,
)
from stonks.engine.sim_broker import IntradaySimBroker, SessionKey, calendar_session_key
from stonks.engine.step import DecisionStep, StepBook, StepDecision
from stonks.execution.reconcile import reconcile_orders
from stonks.logging import get_logger
from stonks.store.state import SqliteState
from stonks.streaming.base import StreamingSource

if TYPE_CHECKING:
    from stonks.backtest.costs import CostModel
    from stonks.backtest.fills import MinuteFillSettings
    from stonks.config import Settings
    from stonks.store.bars import BarStore
    from stonks.streaming.runner import StreamRunner

_log = get_logger("stonks.engine.process")

#: How often an idle engine writes its heartbeat.
HEARTBEAT_EVERY = timedelta(seconds=30)


@dataclass
class EngineBook:
    """One intraday book. ``broker=None`` makes a simulated book, built at
    start from ``initial_cash`` and the ledger's fills."""

    spec: StepBook
    broker: Broker | None = None
    initial_cash: float = 100_000.0
    fill: MinuteFillSettings | None = None
    cost_model: CostModel | None = None
    session_key: SessionKey | None = calendar_session_key

    def __post_init__(self) -> None:
        if not self.spec.portfolio_id:
            raise ValueError(f"engine book {self.spec.id!r} needs a portfolio_id")

    @property
    def id(self) -> str:
        return self.spec.id

    @property
    def portfolio_id(self) -> str:
        return str(self.spec.portfolio_id)

    @property
    def simulated(self) -> bool:
        return self.broker is None or isinstance(self.broker, IntradaySimBroker)


@dataclass(frozen=True)
class RoutedDecision:
    """One book's decision on one bar close and what the router did."""

    decision: StepDecision
    acks: tuple[RouteAck, ...]

    @property
    def orders(self) -> list[Order]:
        return self.decision.orders


@dataclass
class StartReport:
    run_id: str
    crashed: list[EngineRun] = field(default_factory=list)
    #: The crashed run this start resumes, and the close it resumes after.
    recovered_from: str | None = None
    resume_after: datetime | None = None
    lost_orders: dict[str, list[str]] = field(default_factory=dict)
    reconciled: dict[str, int] = field(default_factory=dict)


@dataclass
class EngineStats:
    events: int = 0
    bar_closes: int = 0
    skipped_closes: int = 0
    decisions: int = 0
    orders_routed: int = 0
    orders_sent: int = 0
    orders_known: int = 0
    orders_rejected: int = 0
    orders_held: int = 0
    flatten_orders: int = 0
    step_errors: int = 0
    route_errors: int = 0
    last_close_at: datetime | None = None

    def snapshot(self) -> dict[str, Any]:
        out = dict(self.__dict__)
        out["last_close_at"] = self.last_close_at.isoformat() if self.last_close_at else None
        return out


class EngineProcess:
    """See the module doc. Give either ``source`` (a replay) or ``runner``
    (live). ``on_decision`` sees every routed decision (tests, the console)."""

    name = "engine"

    def __init__(
        self,
        *,
        strategies: Mapping[str, Strategy],
        books: Sequence[EngineBook],
        lake: Any,
        state: SqliteState,
        universe: Sequence[str],
        session: date,
        source: StreamingSource | None = None,
        runner: StreamRunner | None = None,
        clock: Clock = SYSTEM_CLOCK,
        interval: Interval = Interval.MIN_1,
        control: EngineControl | None = None,
        stop_at: datetime | None = None,
        threshold: float = 0.0,
        halts: HaltTable = NO_HALTS,
        stale_after: timedelta | None = None,
        bar_store: BarStore | None = None,
        on_decision: Callable[[RoutedDecision], object] | None = None,
        settle: timedelta | None = None,
    ) -> None:
        if (source is None) == (runner is None):
            raise ValueError("give the engine either a source (replay) or a runner (live)")
        if not books:
            raise ValueError("the engine needs at least one book")
        self.mode: EngineMode = "replay" if source is not None else "live"
        self.books = list(books)
        self.lake = lake
        self.state = state
        self.universe = list(dict.fromkeys(universe))
        self.session = session
        self.clock = clock
        self.interval = interval
        self.control = control
        self.stop_at = stop_at
        self.halts = halts
        self.bar_store = bar_store
        self.stats = EngineStats()
        self.runs = EngineRuns(state, clock=clock)
        self.run_id: str | None = None
        self.resume_after: datetime | None = None
        self._source = source
        self._runner = runner
        self._on_decision = on_decision
        self._stop = threading.Event()
        self._stop_reason: str | None = None
        self._brokers: dict[str, Broker] = {}
        self._routers: dict[str, IntradayRouter] = {}
        self._last_fill: dict[str, int] = {b.id: 0 for b in self.books}
        #: The strategy behind each routed client id (the ledger keeps only
        #: registered strategy ids).
        self._owner: dict[str, str | None] = {}
        self._registered: frozenset[str] = frozenset()
        self._last_heartbeat: datetime | None = None
        stream = source if source is not None else runner.source  # type: ignore[union-attr]
        self.driver = EventDriver(stream, clock=clock, interval=interval, settle=settle)
        self.step = DecisionStep(
            strategies,
            [b.spec for b in self.books],
            lake,
            universe=self.universe,
            portfolio=lambda book_id: self._brokers[book_id].fetch_portfolio(),
            interval=interval,
            threshold=threshold,
            halts=halts,
            stale_after=stale_after,
            fresh_reads=self.mode == "live",
        )
        self.driver.register(self, name=self.name, priority=ROUTER_PRIORITY)

    # ---- lifecycle --------------------------------------------------------------------

    @property
    def started(self) -> bool:
        return self.run_id is not None

    @property
    def stop_reason(self) -> str | None:
        return self._stop_reason

    def broker(self, book_id: str) -> Broker:
        return self._brokers[book_id]

    def router(self, book_id: str) -> IntradayRouter:
        return self._routers[book_id]

    def start(self) -> StartReport:
        """Recover, reconcile, then open the routers. Raises
        ``ReconciliationPendingError`` while an order stays ``unknown``."""
        crashed = self.runs.mark_crashed()
        recovered = next(
            (
                r
                for r in reversed(crashed)
                if r.session_date == self.session and r.mode == self.mode
            ),
            None,
        )
        if recovered is not None:
            resume = self.runs.checkpoint_for(self.session, self.mode)
            self.resume_after = resume.last_close_at if resume is not None else None
        report = StartReport(
            run_id="",
            crashed=crashed,
            recovered_from=recovered.id if recovered is not None else None,
            resume_after=self.resume_after,
        )
        self._registered = frozenset(r["id"] for r in self.state.sql("SELECT id FROM strategies"))
        stamp = f"{self.session.isoformat()}.{os.getpid()}.{int(self.clock.now().timestamp())}"
        for book in self.books:
            if book.broker is None:
                report.lost_orders[book.id] = end_lost_orders(
                    self.state, book.portfolio_id, clock=self.clock
                )
                broker: Broker = IntradaySimBroker(
                    ledger_portfolio(self.state, book.portfolio_id, book.initial_cash),
                    fill=book.fill,
                    cost_model=book.cost_model,
                    interval=self.interval,
                    session_key=book.session_key,
                    clock=self.clock,
                    order_prefix=f"sim-{book.id}-{stamp}",
                )
            else:
                broker = book.broker
            setter = getattr(broker, "set_asset_classes", None)
            if callable(setter):
                setter(self.step.asset_classes)
            self._brokers[book.id] = broker
            self._routers[book.id] = IntradayRouter(
                broker, self.state, portfolio_id=book.portfolio_id, clock=self.clock
            )
            for fill in ledger_fills(self.state, book.portfolio_id):
                self.step.record_fill(book.id, fill.ticker, fill.strategy_id)
                self._last_fill[book.id] = fill.id
        for book in self.books:
            startup = self._routers[book.id].start()
            report.reconciled[book.id] = startup.summary.orders_checked
        self.run_id = self.runs.start(
            self.session, self.mode, pid=os.getpid(), recovered_from=recovered
        )
        report.run_id = self.run_id
        self._last_heartbeat = self.clock.now()
        _log.info(
            "engine.started",
            run_id=self.run_id,
            mode=self.mode,
            books=[b.id for b in self.books],
            recovered_from=report.recovered_from,
            resume_after=self.resume_after.isoformat() if self.resume_after else None,
        )
        return report

    def run(self) -> EngineStats:
        """Start (when not started yet), loop until a stop, then shut down."""
        if not self.started:
            self.start()
        status = "stopped"
        error: str | None = None
        try:
            if self._source is not None:
                self._run_replay(self._source)
            else:
                self._run_live()
        except BaseException as exc:
            status, error = "failed", f"{type(exc).__name__}: {exc}"
            raise
        finally:
            self._shutdown(status, error)
        return self.stats

    def stop(self, reason: str = "requested") -> None:
        """End :meth:`run` soon. Safe from another thread or a handler."""
        if self._stop.is_set():
            return
        self._stop_reason = reason
        self._stop.set()
        _log.info("engine.stopping", reason=reason)
        if self._runner is not None:
            self._runner.stop()
        elif self._source is not None:
            self._source.close()

    # ---- events -----------------------------------------------------------------------

    def on_event(self, event: StreamEvent) -> None:
        """Feed one event (the runner's subscriber in live mode)."""
        if self._stop.is_set():
            return
        self.stats.events += 1
        self.driver.on_event(event)
        self._check_control()

    def on_bar_close(self, event: BarClose) -> list[RoutedDecision]:
        if self.run_id is None:
            raise RuntimeError("the engine is not started: call start() first")
        if self.resume_after is not None and event.at <= self.resume_after:
            self.stats.skipped_closes += 1
            return []
        self.stats.bar_closes += 1
        self.stats.last_close_at = event.at
        self._write_bars(event)
        for book in self.books:
            self._routers[book.id].on_bar_close(event)
            self._learn_fills(book)
        try:
            decisions = self.step.on_bar_close(event)
        except Exception as exc:
            self.stats.step_errors += 1
            _log.warning("engine.step_failed", close=event.at.isoformat(), error=str(exc))
            decisions = self._flatten_only(event)
        routed: list[RoutedDecision] = []
        for decision in decisions:
            try:
                routed.append(self._route(decision))
            except Exception as exc:  # one book's failure never stops the others
                self.stats.route_errors += 1
                _log.error(
                    "engine.route_failed",
                    book=decision.book_id,
                    close=event.at.isoformat(),
                    error=str(exc),
                )
        self.runs.heartbeat(
            self.run_id,
            last_close_at=event.at,
            bar_closes=self.stats.bar_closes,
            orders_routed=self.stats.orders_routed,
        )
        self._last_heartbeat = self.clock.now()
        return routed

    # ---- internals --------------------------------------------------------------------

    def _run_replay(self, source: StreamingSource) -> None:
        for event in source.stream(self.universe):
            self.on_event(event)
            if self._stop.is_set():
                break
        self.driver.finish(drain=source.finite and not self._stop.is_set())

    def _run_live(self) -> None:
        runner = self._runner
        assert runner is not None
        runner.subscribers.append(self.on_event)
        try:
            runner.run()
        finally:
            runner.subscribers.remove(self.on_event)
        # dispatch what has closed, keep the bar in progress
        self._stop.clear()
        self.driver.finish(drain=False)
        self._stop.set()

    def _check_control(self) -> None:
        now = self.clock.now()
        if self.control is not None and self.control.stop_requested():
            self.stop("stop requested")
        elif self.stop_at is not None and now >= self.stop_at:
            self.stop("session over")
        if (
            self.run_id is not None
            and self._last_heartbeat is not None
            and now - self._last_heartbeat >= HEARTBEAT_EVERY
        ):
            self.runs.heartbeat(self.run_id)
            self._last_heartbeat = now

    def _write_bars(self, event: BarClose) -> None:
        if self.bar_store is None or not event.bars:
            return
        from stonks.streaming.writer import bars_frame

        try:
            self.bar_store.upsert(bars_frame(event.bars, self.interval))
        except Exception as exc:  # the runner's writer and the backfill retry it
            _log.warning("engine.bar_write_failed", close=event.at.isoformat(), error=str(exc))

    def _learn_fills(self, book: EngineBook) -> None:
        for fill in ledger_fills(self.state, book.portfolio_id, after_id=self._last_fill[book.id]):
            owner = self._owner.get(fill.client_id, fill.strategy_id)
            self.step.record_fill(book.id, fill.ticker, owner)
            self._last_fill[book.id] = fill.id

    def _flatten_only(self, event: BarClose) -> list[StepDecision]:
        """The flatten orders of every book in its flatten window, for a bar
        where the step failed: an intraday book still ends the day flat."""
        at = event.at.astimezone(UTC)
        as_of = (at - self.interval.to_timedelta()).replace(tzinfo=None)
        marks = self.step.marks
        classes = self.step.asset_classes
        out: list[StepDecision] = []
        for book in self.books:
            rules = book.spec.sessions
            if rules is None or not rules.flatten_at_close:
                continue
            rulebook = SessionRulebook(rules, halts=self.halts)
            positions = self._brokers[book.id].fetch_portfolio().positions
            orders = flatten_orders(
                positions,
                lambda t, rb=rulebook: rb.state(at, t, asset_class=classes.get(t)),
                portfolio_id=book.portfolio_id,
                prices=marks,
            )
            if orders:
                orders = [replace(o, decision_price=marks.get(o.ticker)) for o in orders]
                flat = sorted({o.ticker for o in orders})
                out.append(StepDecision(book.id, at, as_of, orders, flattened=flat))
        return out

    def _route(self, decision: StepDecision) -> RoutedDecision:
        router = self._routers[decision.book_id]
        orders = []
        for order in decision.orders:
            self._owner[order.client_id] = order.strategy_id
            if order.strategy_id is not None and order.strategy_id not in self._registered:
                # orders.strategy_id references the registry: a catalog
                # strategy or a flatten order is stored without one
                order = replace(order, strategy_id=None)
            orders.append(order)
        acks = router.route(orders)
        s = self.stats
        s.decisions += 1
        s.orders_routed += len(acks)
        s.flatten_orders += sum(1 for o in decision.orders if o.strategy_id == "flatten")
        for ack in acks:
            if ack.status == "sent":
                s.orders_sent += 1
            elif ack.status == "known":
                s.orders_known += 1
            elif ack.status == "held":
                s.orders_held += 1
            else:
                s.orders_rejected += 1
        routed = RoutedDecision(decision, acks)
        if self._on_decision is not None:
            self._on_decision(routed)
        return routed

    def _shutdown(self, status: str, error: str | None) -> None:
        for book in self.books:
            broker = self._brokers.get(book.id)
            if broker is None:
                continue
            expire = getattr(broker, "expire_open", None)
            if callable(expire):
                expire("session_end")
            try:
                reconcile_orders(
                    broker, self.state, now=self.clock.now(), portfolio_id=book.portfolio_id
                )
            except Exception as exc:
                _log.warning("engine.final_reconcile_failed", book=book.id, error=str(exc))
        if self.run_id is not None:
            self.runs.finish(
                self.run_id,
                "failed" if status == "failed" else "stopped",
                error=error,
                summary={
                    **self.stats.snapshot(),
                    "stop_reason": self._stop_reason,
                    "handler_errors": dict(self.driver.stats.handler_errors),
                },
            )
        _log.info("engine.stopped", run_id=self.run_id, status=status, **self.stats.snapshot())


# ---- building from settings ------------------------------------------------------------


def session_window(calendar: str, session: date) -> tuple[datetime, datetime] | None:
    """The open and close of ``session`` on ``calendar`` (aware UTC)."""
    from stonks.scheduling.calendar import get_calendar

    s = get_calendar(calendar).session(session)
    return None if s is None else (s.open, s.close)


def resolve_strategies(
    names: Sequence[str], state: SqliteState, settings: Settings
) -> dict[str, Strategy]:
    """Registry ids load the registered strategy (its params and fitted
    state). Any other name is a catalog strategy with default params."""
    from stonks.lab.catalog import resolve_strategy
    from stonks.registry.store import StrategyRegistry

    registry = StrategyRegistry(state, settings.registry.artifacts_dir)
    known = {r["id"] for r in state.sql("SELECT id FROM strategies")}
    out: dict[str, Strategy] = {}
    for name in names:
        if name in known:
            out[name] = registry.load(name)
        else:
            out[name] = resolve_strategy(name)({})  # type: ignore[call-arg]
    return out


BrokerFactory = Callable[["Settings", str], Broker]


def ibkr_intraday_broker(settings: Settings, portfolio_id: str) -> Broker:
    """The IBKR adapter of ``portfolio_id``'s gateway, sending day orders."""
    from stonks.execution.brokers.ibkr.factory import connect_ibkr

    state = SqliteState(settings.state.path)
    try:
        broker = connect_ibkr(
            settings.brokers.ibkr, portfolio_id=portfolio_id, role="tick", state=state
        )
    except Exception:
        state.close()
        raise
    broker.on_close(state.close)
    broker.intraday = True
    return broker  # type: ignore[return-value]


def build_engine(
    settings: Settings,
    *,
    lake: Any,
    state: SqliteState,
    session: date,
    source: StreamingSource | None = None,
    runner: StreamRunner | None = None,
    clock: Clock = SYSTEM_CLOCK,
    control: EngineControl | None = None,
    brokers: Mapping[str, BrokerFactory] | None = None,
    strategies: Mapping[str, Strategy] | None = None,
    write_bars: bool | None = None,
) -> EngineProcess:
    """An :class:`EngineProcess` from ``[engine]``. One strategy set is
    shared by every book (each book trades its own listed strategies)."""
    cfg = settings.engine
    if not cfg.books:
        raise ValueError("[engine] has no books: add [[engine.books]]")
    interval = Interval.parse(cfg.interval)
    universe = cfg.universe or settings.streaming.tickers
    if not universe:
        raise ValueError("[engine] has no universe and [streaming] no tickers")
    factories: dict[str, BrokerFactory] = {"ibkr": ibkr_intraday_broker, **(brokers or {})}
    names = list(dict.fromkeys(n for b in cfg.books for n in b.strategies))
    pool = (
        dict(strategies) if strategies is not None else resolve_strategies(names, state, settings)
    )
    books: list[EngineBook] = []
    for b in cfg.books:
        weights = {n: 1.0 / len(b.strategies) for n in b.strategies}
        spec = StepBook(
            id=b.id,
            portfolio_id=b.portfolio_id,
            construction=b.construction,
            strategy_weights=weights,
            sessions=b.sessions,
        )
        broker = None if b.broker == "simulated" else factories[b.broker](settings, b.portfolio_id)
        books.append(EngineBook(spec=spec, broker=broker, initial_cash=b.initial_cash))
    window = session_window(cfg.calendar, session)
    stop_at = window[1] + timedelta(minutes=cfg.stop_after_close_minutes) if window else None
    live = runner is not None
    bar_store = None
    if write_bars if write_bars is not None else live:
        bar_store = getattr(lake, "bar_store", None)
    return EngineProcess(
        strategies={n: pool[n] for n in names},
        books=books,
        lake=lake,
        state=state,
        universe=universe,
        session=session,
        source=source,
        runner=runner,
        clock=clock,
        interval=interval,
        control=control,
        stop_at=stop_at if live else None,
        threshold=cfg.threshold,
        stale_after=timedelta(seconds=cfg.stale_after_seconds)
        if live and cfg.stale_after_seconds
        else None,
        bar_store=bar_store,
    )

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
    5. marks: intraday P&L (IntradayPnlTracker, 21.3.3), each book's marked
       value, then trip_intraday_loss and the halts in force (event_verdict);
       a stop-all kill switch cancels the book's working orders (21.3.2)
    6. DecisionStep: signals, build_orders with RiskContext.intraday (the
       intraday rules), session gates, flatten orders
    7. per book: trip_intraday_runaway, gate_event_orders, then
       IntradayRouter.route(orders); each sent order feeds the monitor's
       event to order histogram (21.3.4)
    8. checkpoint and heartbeat in engine_runs
```

The :class:`~stonks.engine.monitor.EngineMonitor` (21.3.4) is attached to
the driver, so it measures every dispatch and publishes the
``engine_status`` row the API, ``/metrics`` and the dead-man read.

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
from stonks.core.types import Order, Portfolio
from stonks.engine.control import EngineControl
from stonks.engine.driver import BarClose, EventDriver
from stonks.engine.monitor import EngineMonitor
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
    from stonks.engine.status import EngineStatusStore
    from stonks.production.intraday_halts import EventVerdict
    from stonks.production.intraday_pnl import IntradayPnlTracker
    from stonks.production.rules._intraday import IntradayContext
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
    #: Orders the halts in force stopped at the event (21.3.2).
    orders_halted: int = 0
    #: Halts the engine opened: intraday loss and order bursts.
    halts_tripped: int = 0
    #: Working orders a stop-all kill switch cancelled.
    orders_cancelled: int = 0
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
        status: EngineStatusStore | None = None,
        engine_id: str = "default",
        calendar: str = "XNYS",
        publish_seconds: float = 15.0,
        pnl: IntradayPnlTracker | None = None,
        notify_halts: bool = True,
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
        #: Intraday risk state per book (21.3.2): the session's marked
        #: values, when each order went out, the halts in force now.
        self._equity: dict[str, list[tuple[datetime, float]]] = {b.id: [] for b in self.books}
        self._sent: dict[str, list[datetime]] = {b.id: [] for b in self.books}
        self._intraday: dict[str, IntradayContext] = {}
        self._verdicts: dict[str, EventVerdict] = {}
        self._notify_halts = notify_halts
        self.pnl = pnl
        stream = source if source is not None else runner.source  # type: ignore[union-attr]
        self.driver = EventDriver(stream, clock=clock, interval=interval, settle=settle)
        self.monitor = EngineMonitor(
            self.driver,
            engine_id=engine_id,
            calendar=calendar,
            health=getattr(runner, "health", None),
            store=status,
            publish_seconds=publish_seconds,
        )
        self.monitor.attach()
        self.step = DecisionStep(
            strategies,
            [b.spec for b in self.books],
            lake,
            universe=self.universe,
            portfolio=self.book_portfolio,
            interval=interval,
            threshold=threshold,
            halts=halts,
            stale_after=stale_after,
            fresh_reads=self.mode == "live",
            intraday=lambda book_id, _at: self._intraday.get(book_id),
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

    def book_portfolio(self, book_id: str) -> Portfolio:
        """What ``book_id`` holds. A simulated broker holds the book alone.
        A real broker account can hold the owner's own shares and other
        books' too, so the book sees only what its own fills bought
        (``production.ownership.managed_view``): it never decides on,
        exits or flattens a holding that is not its own."""
        account = self._brokers[book_id].fetch_portfolio()
        book = next(b for b in self.books if b.id == book_id)
        if book.simulated:
            return account
        from stonks.production.ownership import managed_view, owned_positions

        managed, _ = managed_view(account, owned_positions(self.state, book.portfolio_id))
        return managed

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
                broker,
                self.state,
                portfolio_id=book.portfolio_id,
                clock=self.clock,
                hold_working=not book.simulated,
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
        self.monitor.start()
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
        self._after_marks(event)
        try:
            decisions = self.step.on_bar_close(event)
        except Exception as exc:
            self.stats.step_errors += 1
            _log.warning("engine.step_failed", close=event.at.isoformat(), error=str(exc))
            decisions = self._flatten_only(event)
        routed: list[RoutedDecision] = []
        for decision in decisions:
            try:
                routed.append(self._route(decision, event))
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

    # ---- intraday risk (21.3.2) and P&L (21.3.3) ---------------------------------------

    def _after_marks(self, event: BarClose) -> None:
        """After the fills of a bar close: intraday P&L, each book's marked
        value and loss limit, the halts in force, and the kill switch's
        cancels. A failure here is logged; it never stops the loop."""
        from stonks.production.intraday_halts import event_verdict, trip_intraday_loss
        from stonks.production.rules._intraday import IntradayContext

        at = event.at.astimezone(UTC)
        if self.pnl is not None:
            try:
                self.pnl.on_bar_close(event)
            except Exception as exc:
                _log.warning("engine.pnl_failed", close=at.isoformat(), error=str(exc))
        marks = {**self.step.marks, **{b.ticker: b.close for b in event.bars}}
        last_bar = {**self.step.last_bar_at, **{b.ticker: at for b in event.bars}}
        stale = self._stream_stale()
        for book in self.books:
            try:
                portfolio = self.book_portfolio(book.id)
                value = _marked_value(portfolio.cash, portfolio.positions, marks)
                equity = [(t, v) for t, v in self._equity[book.id] if t.date() == at.date()]
                sent = [t for t in self._sent[book.id] if t.date() == at.date()]
                self._sent[book.id] = sent
                self._intraday[book.id] = IntradayContext(
                    now=at,
                    equity_marks=tuple(equity),
                    last_bar_at=last_bar,
                    sent_at=tuple(sent),
                    stream_stale=stale,
                )
                if value is not None:
                    equity.append((at, value))
                self._equity[book.id] = equity
                ctx = self.step.risk_context(book.spec, portfolio, marks, at)
                if ctx is not None and value is not None:
                    halt = trip_intraday_loss(
                        self.state, book.portfolio_id, ctx, notify=self._notify_halts
                    )
                    if halt is not None:
                        self.stats.halts_tripped += 1
                verdict = event_verdict(self.state, book.portfolio_id, now=at)
                self._verdicts[book.id] = verdict
                if verdict.cancel_working:
                    self._cancel_working(book)
            except Exception as exc:
                _log.warning(
                    "engine.intraday_risk_failed",
                    book=book.id,
                    close=at.isoformat(),
                    error=str(exc),
                )

    def _stream_stale(self) -> bool:
        """The live stream has no fresh prices (reconnecting or failed)."""
        health = getattr(self._runner, "health", None)
        return health is not None and health.state in ("backoff", "connecting", "failed")

    def _cancel_working(self, book: EngineBook) -> None:
        from stonks.execution.cancel import cancel_working_orders

        summary = cancel_working_orders(
            self._brokers[book.id],
            self.state,
            portfolio_id=book.portfolio_id,
            now=self.clock.now(),
        )
        self.stats.orders_cancelled += len(summary.cancelled)
        if summary.cancelled:
            _log.warning(
                "engine.kill_switch_cancelled", book=book.id, orders=list(summary.cancelled)
            )

    def _gate_halts(self, decision: StepDecision) -> list[Order]:
        """The decision's orders the halts in force let through: trip the
        order burst halt first (the rate cap dropped an opening order), then
        read the halts again and gate."""
        from stonks.production.intraday_halts import (
            event_verdict,
            gate_event_orders,
            trip_intraday_runaway,
        )

        book = next(b for b in self.books if b.id == decision.book_id)
        at = decision.at.astimezone(UTC)
        verdict = self._verdicts.get(book.id)
        adjustments = decision.result.adjustments if decision.result is not None else []
        if adjustments:
            halt = trip_intraday_runaway(
                self.state,
                book.portfolio_id,
                adjustments,
                on=at.date(),
                notify=self._notify_halts,
            )
            if halt is not None:
                self.stats.halts_tripped += 1
                verdict = event_verdict(self.state, book.portfolio_id, now=at)
                self._verdicts[book.id] = verdict
        if verdict is None or verdict.mode is None or not decision.orders:
            return list(decision.orders)
        positions = self.book_portfolio(book.id).positions
        kept, blocked = gate_event_orders(decision.orders, verdict, positions)
        if blocked:
            self.stats.orders_halted += len(blocked)
            _log.warning(
                "engine.orders_halted",
                book=book.id,
                mode=verdict.mode,
                orders=[o.client_id for o in blocked],
                reasons=list(verdict.reasons),
            )
        return kept

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
            positions = self.book_portfolio(book.id).positions
            orders = flatten_orders(
                positions,
                lambda t, rb=rulebook: rb.state(at, t, asset_class=classes.get(t)),
                portfolio_id=book.portfolio_id,
                prices=marks,
            )
            if orders:
                orders = [
                    self.step.with_interval(replace(o, decision_price=marks.get(o.ticker)))
                    for o in orders
                ]
                flat = sorted({o.ticker for o in orders})
                out.append(StepDecision(book.id, at, as_of, orders, flattened=flat))
        return out

    def _route(self, decision: StepDecision, event: BarClose | None = None) -> RoutedDecision:
        router = self._routers[decision.book_id]
        orders = []
        for order in self._gate_halts(decision):
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
                self._sent[decision.book_id].append(decision.at.astimezone(UTC))
                if event is not None:
                    self.monitor.record_order(event, self.clock.now())
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
        if self.pnl is not None:
            try:
                self.pnl.finish()
            except Exception as exc:
                _log.warning("engine.pnl_finish_failed", error=str(exc))
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
        self.monitor.stop()
        _log.info("engine.stopped", run_id=self.run_id, status=status, **self.stats.snapshot())


def _marked_value(
    cash: float, positions: Mapping[str, float], marks: Mapping[str, float]
) -> float | None:
    """Cash plus positions at their marks, ``None`` when a holding has no
    mark (an unknown value never reads as a loss)."""
    value = float(cash)
    for ticker, quantity in positions.items():
        if not quantity:
            continue
        mark = marks.get(ticker)
        if mark is None:
            return None
        value += float(quantity) * float(mark)
    return value


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
            # ``[production.risk]``: the caps and the intraday rules
            # (``[production.risk.rules.intraday_*]``, 21.3.2)
            risk=settings.production.risk,
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
    from stonks.engine.status import EngineStatusStore

    pnl = None
    if settings.production.intraday_pnl.enabled:
        from stonks.production.intraday_pnl import IntradayPnlTracker, lake_reference_prices

        pnl = IntradayPnlTracker(
            state,
            [b.portfolio_id for b in books],
            reference_prices=lake_reference_prices(lake),
            settings=settings.production.intraday_pnl,
            clock=clock,
        )
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
        status=EngineStatusStore(settings.state.path),
        calendar=cfg.calendar,
        publish_seconds=settings.streaming.monitor.publish_seconds,
        pnl=pnl,
    )

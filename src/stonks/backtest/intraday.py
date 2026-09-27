"""The intraday backtest on the event driver (roadmap 21.2.2).

:class:`IntradayBacktester` replays lake bars through the same loop a live
run uses:

```
LakeBarSource -> EventDriver -> fills (priority -10) -> DecisionStep (0)
```

- :class:`~stonks.streaming.sources.lake_bars.LakeBarSource` yields the
  window's bars in time order and moves a
  :class:`~stonks.core.clock.FakeClock`.
- :class:`~stonks.engine.driver.EventDriver` groups them into bar closes.
- On each bar close the fills handler runs first: it fills the orders
  queued on the previous close at this bar's opens (P21), marks the book at
  the closes (carried forward per ticker) and records equity.
- Then :class:`~stonks.engine.step.DecisionStep` decides through
  ``build_orders`` with a point-in-time view of the lake and queues the
  orders for the next bar. A new decision replaces what is still queued.

The execution convention matches :class:`~stonks.backtest.engine.Backtester`
exactly (next bar open, carried remainders, financing accrual per bar), so
on the same minute bars and the same strategies the two give the same
fills and the same equity curve. The unit tests hold that parity.

Not here yet: corporate actions, point-in-time membership, the broker's
lagged market statistics and forced margin closes (the bar backtester
keeps them), and the intraday router of 21.2.3, which will replace the
fills handler.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field, replace
from datetime import UTC, date, datetime, time, timedelta
from typing import TYPE_CHECKING, Any

from stonks.backtest.calendar import calendar_for_universe
from stonks.backtest.report import BacktestReport, compute_report, periods_per_year
from stonks.backtest.simulated_broker import SimulatedBroker
from stonks.core.clock import FakeClock
from stonks.core.interval import Interval
from stonks.core.protocols import Strategy
from stonks.core.types import AssetClass, Order
from stonks.engine.driver import BarClose, DriverStats, EventDriver
from stonks.engine.sessions import NO_HALTS, HaltTable, SessionRules
from stonks.engine.step import DecisionStep, StepBook, StepDecision
from stonks.logging import get_logger
from stonks.portfolio.settings import ConstructionSettings
from stonks.streaming.sources.lake_bars import LakeBarSource

if TYPE_CHECKING:
    from stonks.config import RiskPolicy

_log = get_logger("stonks.backtest.intraday")

#: The one book of an intraday backtest.
BOOK_ID = "backtest"


@dataclass(frozen=True)
class IntradayBacktestConfig:
    """``start`` and ``end`` are days (the whole day, UTC) or aware or naive
    UTC datetimes (``end`` exclusive: bars that close by it)."""

    start: date | datetime
    end: date | datetime
    universe: Sequence[str]
    interval: Interval = Interval.MIN_1
    threshold: float = 0.0
    construction: str | ConstructionSettings = field(default_factory=ConstructionSettings)
    #: Capital share per strategy, in strategy order (``None`` = equal).
    strategy_weights: Sequence[float] | None = None
    risk: RiskPolicy | None = None
    #: ``None``: no session gate (parity with the bar backtester).
    sessions: SessionRules | None = None
    halts: HaltTable = NO_HALTS

    def __post_init__(self) -> None:
        if not self.interval.is_intraday:
            raise ValueError(
                f"the intraday backtest needs an intraday interval, got {self.interval.code}"
            )
        if isinstance(self.construction, str):
            object.__setattr__(self, "construction", ConstructionSettings(method=self.construction))

    @property
    def construction_settings(self) -> ConstructionSettings:
        return self.construction  # type: ignore[return-value]

    def window(self) -> tuple[datetime, datetime]:
        """The source window as aware UTC instants."""
        return _instant(self.start, end=False), _instant(self.end, end=True)


def _instant(value: date | datetime, *, end: bool) -> datetime:
    if isinstance(value, datetime):
        return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)
    day = value + timedelta(days=1) if end else value
    return datetime.combine(day, time.min, tzinfo=UTC)


class IntradayBacktester:
    def __init__(
        self,
        strategies: Sequence[Strategy],
        broker: SimulatedBroker,
        lake: Any,
        config: IntradayBacktestConfig,
    ) -> None:
        if not strategies:
            raise ValueError("the intraday backtest needs at least one strategy")
        self._strategies = list(strategies)
        self._broker = broker
        self._lake = lake
        self._config = config
        self.decisions: list[StepDecision] = []
        #: The close each order was decided at, by client id (TCA).
        self.decision_prices: dict[str, float] = {}
        self.driver_stats: DriverStats | None = None
        self._reset()

    def _reset(self) -> None:
        self.decisions = []
        self.decision_prices = {}
        self._pending: list[Order] = []
        self._decided_at: dict[str, datetime] = {}
        self._roots: dict[str, str] = {}
        self._parts: dict[str, int] = {}
        self._marks: dict[str, float] = {}
        self._traded: set[str] = set()
        self._equity_dates: list[datetime] = []
        self._equity: list[float] = []

    # ---- the run -----------------------------------------------------------------

    def run(self) -> BacktestReport:
        self._reset()
        config = self._config
        if bool(getattr(self._broker, "allow_short", False)):
            raise ValueError("the intraday backtest does not run short books yet")
        start, end = config.window()
        clock = FakeClock(start)
        source = LakeBarSource(
            self._lake, start=start, end=end, interval=config.interval, clock=clock
        )
        driver = EventDriver(source, clock=clock, interval=config.interval)
        keys = [str(i) for i in range(len(self._strategies))]
        weights = config.strategy_weights
        book = StepBook(
            id=BOOK_ID,
            construction=config.construction_settings,
            risk=config.risk,
            strategy_weights=None if weights is None else dict(zip(keys, weights, strict=True)),
            sessions=config.sessions,
        )
        step = DecisionStep(
            dict(zip(keys, self._strategies, strict=True)),
            [book],
            self._lake,
            universe=config.universe,
            portfolio=lambda _book: self._broker.fetch_portfolio(),
            interval=config.interval,
            on_decision=self._queue,
            threshold=config.threshold,
            halts=config.halts,
        )
        self._step = step
        asset_classes: dict[str, AssetClass] = step.asset_classes  # type: ignore[assignment]
        self._broker.set_asset_classes(asset_classes)
        self._broker.set_interval(config.interval)
        driver.register(self._on_bar_close, name="fills", priority=-10)
        driver.register(step, name=step.name)
        self.driver_stats = driver.run(list(config.universe))
        if self.driver_stats.total_handler_errors:
            raise RuntimeError(
                f"intraday backtest handlers failed: {self.driver_stats.handler_errors}"
            )
        if self._pending:
            _log.debug("unfilled_orders_at_end", count=len(self._pending))
        classes: set[AssetClass] = {asset_classes.get(t, "equity") for t in self._traded}
        return compute_report(
            ",".join(s.id for s in self._strategies),
            self._equity_dates,
            self._equity,
            periods_per_year=periods_per_year(config.interval, classes),
            sessions_per_year=calendar_for_universe(classes).sessions_per_year,
        )

    # ---- handlers ------------------------------------------------------------------

    def _on_bar_close(self, event: BarClose) -> None:
        """Fills at this bar's opens, then marks and equity at its closes."""
        as_of = (event.at - event.interval.to_timedelta()).astimezone(UTC).replace(tzinfo=None)
        self._broker.accrue(as_of)
        if self._pending:
            self._pending = self._fill(event, as_of)
        for bar in event.bars:
            self._marks[bar.ticker] = bar.close
            self._traded.add(bar.ticker)
        self._broker.set_prices(dict(self._marks), as_of=as_of)
        self._equity_dates.append(as_of)
        self._equity.append(self._broker.fetch_portfolio().total_value(self._marks))

    def _queue(self, decision: StepDecision) -> None:
        """A new decision replaces every queued order."""
        self.decisions.append(decision)
        self._pending = list(decision.orders)
        self._decided_at = {o.client_id: decision.as_of for o in self._pending}
        for order in self._pending:
            if order.decision_price is not None:
                self.decision_prices.setdefault(order.client_id, order.decision_price)

    def _fill(self, event: BarClose, as_of: datetime) -> list[Order]:
        bars = {b.ticker: b for b in event.bars if b.open > 0}
        self._broker.set_prices(
            {t: b.open for t, b in bars.items()},
            as_of=as_of,
            volumes={t: float(b.volume) for t, b in bars.items()},
            highs={t: b.high for t, b in bars.items()},
            lows={t: b.low for t, b in bars.items()},
        )
        waiting: list[Order] = []
        for order in self._pending:
            if order.ticker not in bars:
                waiting.append(order)
                continue
            fill = self._broker.place_order(order, decided_at=self._decided_at.get(order.client_id))
            if fill is not None:
                self._step.record_fill(BOOK_ID, order.ticker, order.strategy_id)
            rest = self._broker.unfilled_quantity(order.client_id)
            if rest > 0:
                waiting.append(self._carry(order, rest, as_of))
        return waiting

    def _carry(self, order: Order, quantity: float, as_of: datetime) -> Order:
        """The deferred ``quantity`` of ``order`` as a child order (the bar
        backtester's naming, ``<root>~<n>``)."""
        root = self._roots.get(order.client_id, order.client_id)
        part = self._parts.get(root, 1) + 1
        self._parts[root] = part
        child = replace(order, client_id=f"{root}~{part}", quantity=quantity)
        self._roots[child.client_id] = root
        self._decided_at[child.client_id] = as_of
        return child

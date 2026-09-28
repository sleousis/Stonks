"""Paper books fill at the next session's open, like the backtest (P21).

A backtest decides on a bar's close and fills at the next bar's open
(``backtest/engine.py``). A paper book (a simulated book of the tick) does
the same. The tick that decides records each order as working
(``pending``, state ``accepted``) and fills nothing. The next tick, before
it decides, sweeps those orders: each one fills in the first daily bar of
its ticker after the decision day, through the same ``SimulatedBroker``,
fill model (``[backtest.execution]``) and cost model (``[backtest.costs]``)
as a backtest. The broker sees the bar's open, high, low and volume and the
lagged market statistics of that bar, so the participation cap, the gap
guard, limit orders and the cost model's impact work exactly as in a
backtest.

What a sweep leaves behind for every order it looks at:

- ``filled``: it filled in full, or the broker cut it to cash or to the
  held position. The row then records what traded (TO-11).
- ``cancelled``: the fill model filled part of it (participation cap, zero
  volume) or none of it and would carry the rest. The backtest replaces a
  carry with the next decision, and so does the paper book: the fill is
  kept and the rest is cancelled, "replaced by the next decision". An
  order whose ticker had no bar since the decision is cancelled the same
  way.
- ``expired``: the fill model refused it with nothing to carry (the gap
  guard, a limit not reached, no cash).

An order decided on the sweep's own day keeps working. A split whose
ex-date falls between the decision and the fill bar rescales the order,
as the backtest rescales its queue.

``[production] paper_fills = "close"`` keeps the old convention (fill at
once at the latest close) for installs that want it.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, replace
from datetime import UTC, date, datetime, time, timedelta
from typing import TYPE_CHECKING, Any, Literal

from stonks.backtest.fills import MarketStats, lagged_market_stats, market_stats_from_row
from stonks.core.corporate_actions import CorporateActions, Split
from stonks.core.timeutil import day_start
from stonks.core.types import Fill, Order, Portfolio
from stonks.logging import get_logger
from stonks.production.ledger import ledger_columns, ledger_filter

if TYPE_CHECKING:
    from stonks.backtest.simulated_broker import SimulatedBroker
    from stonks.store.lake import DuckDBLake
    from stonks.store.state import SqliteState

_log = get_logger("stonks.production.paper_fills")

#: How a simulated book fills: at the next session's open (the backtest's
#: convention) or at once at the latest close (the old convention).
PaperFillMode = Literal["next_open", "close"]
SweepStatus = Literal["filled", "cancelled", "expired"]

_QTY_EPS = 1e-9
REPLACED = "replaced by the next decision"


@dataclass(frozen=True)
class OpenBar:
    """The daily bar a working order may fill in, in major currency units."""

    open: float
    high: float | None = None
    low: float | None = None
    volume: float | None = None
    #: Lagged statistics of this bar (``None``: the models need none).
    stats: MarketStats | None = None


@dataclass(frozen=True)
class WorkingOrder:
    order: Order
    #: The session whose close the order was decided at.
    decided_on: date


@dataclass(frozen=True)
class OpenFill:
    """What a sweep made of one working order."""

    #: The order as the ledger should record it (cut to what traded when
    #: the broker scaled it to cash or position).
    order: Order
    fill: Fill | None
    status: SweepStatus
    reason: str | None = None
    #: The pre-cost price the fill was priced from (the open): the TCA
    #: arrival price.
    arrival: float | None = None
    #: The bar it filled in (``None``: its ticker had no bar).
    bar_day: date | None = None


def fill_at_next_open(
    broker: SimulatedBroker,
    working: Sequence[WorkingOrder],
    bars: Mapping[date, Mapping[str, OpenBar]],
    *,
    as_of: date,
    actions: CorporateActions | None = None,
) -> list[OpenFill]:
    """Fill each order decided before ``as_of`` in the first bar of its
    ticker after its decision day (see the module doc). ``bars`` are keyed
    by day, then ticker. Orders fill day by day, in the order given within
    a day (sells before buys when the tick recorded them so). Orders
    decided on ``as_of`` are left out of the result."""
    due = [w for w in working if w.decided_on < as_of]
    first_bar: dict[str, date | None] = {}
    by_day: dict[date, list[WorkingOrder]] = {}
    for w in due:
        day = next(
            (d for d in sorted(bars) if w.decided_on < d <= as_of and w.order.ticker in bars[d]),
            None,
        )
        first_bar[w.order.client_id] = day
        if day is not None:
            by_day.setdefault(day, []).append(w)
    results: dict[str, OpenFill] = {}
    for day in sorted(by_day):
        row = bars[day]
        broker.set_prices(
            {t: b.open for t, b in row.items()},
            day,
            volumes={t: b.volume for t, b in row.items() if b.volume is not None},
            highs={t: b.high for t, b in row.items() if b.high is not None},
            lows={t: b.low for t, b in row.items() if b.low is not None},
            stats={t: b.stats for t, b in row.items() if b.stats is not None},
        )
        for w in by_day[day]:
            order = _split_adjusted(w.order, actions, w.decided_on, day)
            decided = datetime.combine(w.decided_on, time(), UTC)
            fill = broker.place_order(order, decided_at=decided)
            carry = broker.unfilled_quantity(order.client_id)
            results[order.client_id] = _outcome(broker, order, fill, carry, day)
    out: list[OpenFill] = []
    for w in due:
        cid = w.order.client_id
        if cid in results:
            out.append(results[cid])
        else:
            out.append(OpenFill(order=w.order, fill=None, status="cancelled", reason=_NO_BAR))
    return out


_NO_BAR = f"no bar since the decision; {REPLACED}"


def _outcome(
    broker: SimulatedBroker, order: Order, fill: Fill | None, carry: float, day: date
) -> OpenFill:
    arrival = broker.reference_price(order.client_id) if fill is not None else None
    if fill is None:
        if carry > _QTY_EPS:
            return OpenFill(
                order, None, "cancelled", f"not filled at the open; {REPLACED}", bar_day=day
            )
        return OpenFill(order, None, "expired", "not filled at the next open", bar_day=day)
    if carry > _QTY_EPS:
        reason = f"filled {fill.quantity:g} of {order.quantity:g} at the open; {REPLACED}"
        return OpenFill(order, fill, "cancelled", reason, arrival, day)
    if fill.quantity < order.quantity - _QTY_EPS:
        # scaled down to cash or position: the row records what traded (TO-11)
        reason = f"filled {fill.quantity:g} of {order.quantity:g} requested"
        return OpenFill(
            replace(order, quantity=fill.quantity), fill, "filled", reason, arrival, day
        )
    return OpenFill(order, fill, "filled", None, arrival, day)


def _split_adjusted(
    order: Order, actions: CorporateActions | None, decided_on: date, fill_day: date
) -> Order:
    if actions is None:
        return order
    ratio = 1.0
    for event in actions.by_ticker.get(order.ticker, ()):
        if isinstance(event, Split) and decided_on < event.ex_date <= fill_day:
            ratio *= event.ratio
    if ratio == 1.0:
        return order
    limit = order.limit_price / ratio if order.limit_price is not None else None
    stop = order.stop_price / ratio if order.stop_price is not None else None
    return replace(order, quantity=order.quantity * ratio, limit_price=limit, stop_price=stop)


# ---- the ledger ------------------------------------------------------------------------


def load_working_orders(state: SqliteState, portfolio_id: str | None) -> list[WorkingOrder]:
    """The tick's orders of ``portfolio_id`` still working, oldest first
    (protective stops and manual orders are left out: they have their own
    paths)."""
    cols = ledger_columns(state, "orders")
    where, params = ledger_filter(state, "orders", portfolio_id)
    clauses = [where, "status IN ('pending', 'partially_filled')", "tick_id IS NOT NULL"]
    clauses.append("tick_id IN (SELECT id FROM tick_runs)")
    if "protective" in cols:
        clauses.append("COALESCE(protective, 0) = 0")
    if "origin" in cols:
        clauses.append("origin = 'strategy'")
    wanted = [
        "client_id",
        "ticker",
        "side",
        "quantity",
        "order_type",
        "limit_price",
        "strategy_id",
        "tick_id",
        "created_at",
    ]
    wanted += [c for c in ("decided_at", "position_effect", "stop_price") if c in cols]
    rows = state.sql(
        f"SELECT {', '.join(wanted)} FROM orders WHERE {' AND '.join(clauses)}"
        " ORDER BY created_at, rowid",
        params,
    )
    out: list[WorkingOrder] = []
    for r in rows:
        row = dict(r)
        decided = _parse(row.get("decided_at")) or _parse(row["created_at"])
        order = Order(
            client_id=row["client_id"],
            ticker=row["ticker"],
            side=row["side"],
            quantity=float(row["quantity"]),
            order_type=row["order_type"],
            limit_price=row["limit_price"],
            strategy_id=row["strategy_id"],
            tick_id=row["tick_id"],
            portfolio_id=portfolio_id,
            decided_at=decided,
            position_effect=row.get("position_effect"),
            stop_price=row.get("stop_price"),
        )
        out.append(WorkingOrder(order=order, decided_on=(decided or datetime.now(UTC)).date()))
    return out


def load_open_bars(
    lake: DuckDBLake,
    tickers: Sequence[str],
    after: date,
    through: date,
    *,
    lookback_bars: int = 0,
    stats_spec: Any = None,
) -> dict[date, dict[str, OpenBar]]:
    """The daily bars of ``tickers`` dated after ``after`` through
    ``through``, in major currency units, with each bar's lagged
    statistics when ``stats_spec`` (a ``MarketStatsSpec``) is given. The
    query reads ``lookback_bars`` more bars per ticker so the statistics
    see the same windows as a backtest's."""
    from stonks.fx.units import price_scales

    if not tickers or through <= after:
        return {}
    span = (through - after).days + 1
    frame = lake.sql(
        """
        SELECT ticker, timestamp, open, high, low, close, adj_close, volume
          FROM bars
         WHERE interval = '1d' AND ticker = ANY(?) AND timestamp < ?
        QUALIFY ROW_NUMBER() OVER (PARTITION BY ticker ORDER BY timestamp DESC) <= ?
         ORDER BY timestamp, ticker
        """,
        [sorted(set(tickers)), day_start(through + timedelta(days=1)), span + lookback_bars],
    )
    if frame.empty:
        return {}
    stats = lagged_market_stats(frame, stats_spec) if stats_spec is not None else None
    scales = price_scales(lake, tickers)
    out: dict[date, dict[str, OpenBar]] = {}
    for i, rec in enumerate(frame.to_dict("records")):
        stamp = rec["timestamp"]
        day = stamp.date() if isinstance(stamp, datetime) else stamp
        if hasattr(day, "date") and not isinstance(day, date):
            day = day.date()
        if not after < day <= through:
            continue
        open_ = _num(rec["open"])
        if open_ is None or open_ <= 0:
            continue
        ticker = str(rec["ticker"])
        scale = scales.get(ticker, 1.0)
        high, low = _num(rec["high"]), _num(rec["low"])
        row_stats = None
        if stats is not None:
            s = stats.iloc[i]
            row_stats = market_stats_from_row(s["adv"], s["sigma_daily"], s["half_spread_bps"])
        out.setdefault(day, {})[ticker] = OpenBar(
            open=open_ * scale,
            high=None if high is None else high * scale,
            low=None if low is None else low * scale,
            volume=_num(rec["volume"]),
            stats=row_stats,
        )
    return out


@dataclass(frozen=True)
class PaperSweep:
    """One book's sweep: the outcomes and the ids it settled."""

    outcomes: tuple[OpenFill, ...] = ()

    @property
    def settled(self) -> frozenset[str]:
        return frozenset(o.order.client_id for o in self.outcomes)

    @property
    def fills(self) -> int:
        return sum(1 for o in self.outcomes if o.fill is not None)

    def as_dict(self) -> dict[str, Any]:
        counts: dict[str, int] = {}
        for o in self.outcomes:
            counts[o.status] = counts.get(o.status, 0) + 1
        return {"paper_fills": {"fills": self.fills, **counts}} if self.outcomes else {}


def sweep_paper_orders(
    state: SqliteState,
    lake: DuckDBLake,
    portfolio: Portfolio,
    *,
    portfolio_id: str | None,
    as_of: date,
    make_broker: Callable[[Portfolio], SimulatedBroker],
    actions: CorporateActions | None = None,
) -> PaperSweep:
    """Fill a paper book's working orders at the next open (module doc).
    ``portfolio`` changes in place. Nothing is written: the caller records
    the outcomes with :func:`record_open_fills` in the transaction that
    writes the run's snapshot."""
    working = load_working_orders(state, portfolio_id)
    due = [w for w in working if w.decided_on < as_of]
    if not due:
        return PaperSweep()
    from stonks.core.interval import Interval
    from stonks.production.tick import _asset_classes

    tickers = sorted({w.order.ticker for w in due})
    broker = make_broker(portfolio)
    broker.set_interval(Interval.DAY_1)
    broker.set_asset_classes(_asset_classes(lake, tickers))  # type: ignore[arg-type]
    spec = broker.market_stats_spec
    bars = load_open_bars(
        lake,
        tickers,
        min(w.decided_on for w in due),
        as_of,
        lookback_bars=spec.lookback_bars if spec is not None else 0,
        stats_spec=spec,
    )
    outcomes = fill_at_next_open(broker, due, bars, as_of=as_of, actions=actions)
    for o in outcomes:
        _log.info(
            "paper.open_fill",
            portfolio_id=portfolio_id,
            client_id=o.order.client_id,
            status=o.status,
            price=o.fill.price if o.fill else None,
            quantity=o.fill.quantity if o.fill else 0.0,
        )
    return PaperSweep(outcomes=tuple(outcomes))


def record_open_fills(state: SqliteState, sweep: PaperSweep, *, portfolio_id: str | None) -> None:
    """Write the sweep's fills and move each order to its final state.
    Call inside the transaction that writes the run's snapshot."""
    from stonks.execution.order_state import current_state, write_state
    from stonks.production.tick import _record_fill

    for o in sweep.outcomes:
        cid = o.order.client_id
        if current_state(state, cid) in ("filled", "cancelled", "expired", "rejected"):
            continue  # a concurrent writer settled it
        if o.fill is not None:
            _record_fill(state, o.fill, portfolio_id=portfolio_id, arrival_price=o.arrival)
            state.execute(
                "UPDATE orders SET quantity = ? WHERE client_id = ?", [o.order.quantity, cid]
            )
            if o.status != "filled":
                write_state(state, cid, "partially_filled")
        write_state(state, cid, o.status, reason=o.reason)


def _parse(raw: Any) -> datetime | None:
    if not raw:
        return None
    try:
        value = datetime.fromisoformat(str(raw))
    except ValueError:
        return None
    return value if value.tzinfo is not None else value.replace(tzinfo=UTC)


def _num(value: Any) -> float | None:
    if value is None:
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return None if number != number else number


__all__ = [
    "REPLACED",
    "OpenBar",
    "OpenFill",
    "PaperFillMode",
    "PaperSweep",
    "WorkingOrder",
    "fill_at_next_open",
    "load_open_bars",
    "load_working_orders",
    "record_open_fills",
    "sweep_paper_orders",
]

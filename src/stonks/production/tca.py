"""Transaction cost analysis and the trade journal (BL-32; principles P21,
P22 and P42).

Every order the tick places carries its decision: the price the strategy
decided at (``decision_price``, the latest close), when (``decided_at``),
why (``decision_context``: trigger, strategy, signal score and rank,
constructor, target weight) and what the cost model expected it to cost
(``expected_cost_bps``). :func:`annotate_orders` fills them in; the tick
only records them.

Implementation shortfall (Perold) per order, in currency and in bps of the
filled quantity's value at the decision price. With ``s = +1`` for a buy and
``-1`` for a sell, decision price ``D``, arrival price ``A`` (the market
price when the order reached it, before costs) and fill price ``F``
(volume-weighted over the order's fills)::

    delay    = s (A - D) q     the market moved before the order arrived
    impact   = s (F - A) q     spread, slippage and impact paid to trade
    fees     = the fees charged
    is       = delay + impact + fees = s (F - D) q + fees

The unfilled part of an order costs ``opportunity = s (C - D) u``, with
``C`` the close of the session after the decision and ``u`` the unfilled
quantity. ``convention = s (A - B) q``, with ``B`` the next session's open
(the price a backtest fills at), measures how far the live fill convention
sits from the backtest's (P21): positive means live paid more.

Positive numbers are costs; a favourable move is negative. Aggregates
(:func:`summarize`) add currency and divide by the summed notional, so
large orders weigh more. The same :func:`compute_shortfall` prices
backtest fills (:func:`backtest_shortfalls`), so live and backtest costs are
comparable.

Benchmark prices (``B`` and ``C``) arrive a session later:
:func:`refresh_benchmarks` fills them from the lake once the next bar is
ingested (the ``tca`` tick hook runs it after every tick). For an external
broker it also fills unknown arrival prices with the next open.
"""

from __future__ import annotations

import json
import math
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, replace
from datetime import UTC, date, datetime
from typing import TYPE_CHECKING, Any, Literal

import stonks.accounts  # noqa: F401 - loads before the ledger (import cycle via config)
from stonks.backtest.costs import CostModel, Trade
from stonks.core.types import Fill, Order, OrderSide
from stonks.logging import get_logger
from stonks.production.ledger import ledger_columns, ledger_filter

if TYPE_CHECKING:
    from stonks.store.lake import DuckDBLake
    from stonks.store.state import SqliteState

_log = get_logger("stonks.production.tca")

_BPS = 10_000.0

#: Why an order was placed. ``signal``: a strategy's pick (or its target
#: book); ``exit_no_pick``: the owner of the holdings exited with no pick;
#: ``risk_rule``: a risk rule created it (e.g. a maximum holding time);
#: ``manual``: a person placed it.
DecisionTrigger = Literal["signal", "exit_no_pick", "risk_rule", "manual", "stop"]

#: How :func:`summarize` groups orders.
GroupBy = Literal["all", "strategy", "ticker", "portfolio", "day", "week", "month"]
GROUP_BYS: tuple[GroupBy, ...] = ("all", "strategy", "ticker", "portfolio", "day", "week", "month")


# ---- shortfall math ---------------------------------------------------------------


@dataclass(frozen=True)
class FillLeg:
    """One fill of an order."""

    quantity: float
    price: float
    fee: float = 0.0
    #: The market price when this fill's order arrived (before costs).
    arrival_price: float | None = None


@dataclass(frozen=True)
class Shortfall:
    """Implementation shortfall of one order (see the module doc).

    Currency amounts are ``None`` when an input is not known yet; ``*_bps``
    figures divide by the filled quantity's value at the decision price
    (``opportunity_bps`` by the unfilled part's, ``total_bps`` by the whole
    order's)."""

    side: OrderSide
    ordered_quantity: float
    filled_quantity: float
    decision_price: float
    arrival_price: float | None
    fill_price: float | None
    benchmark_price: float | None
    post_close_price: float | None
    expected_bps: float | None
    delay_cost: float | None
    impact_cost: float | None
    fee_cost: float
    is_cost: float | None
    opportunity_cost: float | None
    convention_cost: float | None

    @property
    def filled_notional(self) -> float:
        """The filled quantity valued at the decision price."""
        return self.filled_quantity * self.decision_price

    @property
    def unfilled_quantity(self) -> float:
        return max(self.ordered_quantity - self.filled_quantity, 0.0)

    @property
    def decision_notional(self) -> float:
        return self.ordered_quantity * self.decision_price

    def _bps(self, cost: float | None, notional: float) -> float | None:
        if cost is None or notional <= 0:
            return None
        return cost / notional * _BPS

    @property
    def delay_bps(self) -> float | None:
        return self._bps(self.delay_cost, self.filled_notional)

    @property
    def impact_bps(self) -> float | None:
        return self._bps(self.impact_cost, self.filled_notional)

    @property
    def fee_bps(self) -> float | None:
        return self._bps(self.fee_cost, self.filled_notional)

    @property
    def is_bps(self) -> float | None:
        return self._bps(self.is_cost, self.filled_notional)

    @property
    def convention_bps(self) -> float | None:
        return self._bps(self.convention_cost, self.filled_notional)

    @property
    def opportunity_bps(self) -> float | None:
        return self._bps(self.opportunity_cost, self.unfilled_quantity * self.decision_price)

    @property
    def total_cost(self) -> float | None:
        """Shortfall plus opportunity cost; ``None`` until both are known."""
        if self.opportunity_cost is None:
            return None
        return (self.is_cost or 0.0) + self.opportunity_cost

    @property
    def total_bps(self) -> float | None:
        return self._bps(self.total_cost, self.decision_notional)

    def as_dict(self) -> dict[str, Any]:
        return {
            "side": self.side,
            "ordered_quantity": self.ordered_quantity,
            "filled_quantity": self.filled_quantity,
            "decision_price": self.decision_price,
            "arrival_price": self.arrival_price,
            "fill_price": self.fill_price,
            "benchmark_price": self.benchmark_price,
            "post_close_price": self.post_close_price,
            "expected_bps": self.expected_bps,
            "delay_bps": self.delay_bps,
            "impact_bps": self.impact_bps,
            "fee_bps": self.fee_bps,
            "is_bps": self.is_bps,
            "opportunity_bps": self.opportunity_bps,
            "convention_bps": self.convention_bps,
            "total_bps": self.total_bps,
            "is_cost": self.is_cost,
            "opportunity_cost": self.opportunity_cost,
        }


def _sign(side: OrderSide) -> float:
    return 1.0 if side == "buy" else -1.0


def compute_shortfall(
    side: OrderSide,
    decision_price: float,
    ordered_quantity: float,
    fills: Sequence[FillLeg],
    *,
    arrival_price: float | None = None,
    benchmark_price: float | None = None,
    post_close_price: float | None = None,
    expected_bps: float | None = None,
) -> Shortfall:
    """Shortfall of one order from its fills. ``arrival_price`` stands in
    for fills that don't carry their own."""
    if decision_price <= 0:
        raise ValueError(f"decision_price must be positive, got {decision_price}")
    s = _sign(side)
    legs = [f for f in fills if f.quantity > 0]
    q = sum(f.quantity for f in legs)
    fees = sum(f.fee for f in legs)
    fill_price = sum(f.quantity * f.price for f in legs) / q if q > 0 else None
    arrivals = [f.arrival_price if f.arrival_price is not None else arrival_price for f in legs]
    if legs and all(a is not None for a in arrivals):
        arrival: float | None = sum(f.quantity * a for f, a in zip(legs, arrivals, strict=True)) / q  # type: ignore[operator]
    else:
        arrival = arrival_price if not legs else None

    is_cost = delay = impact = convention = None
    if fill_price is not None:
        price_cost = s * (fill_price - decision_price) * q
        is_cost = price_cost + fees
        if arrival is not None:
            delay = s * (arrival - decision_price) * q
            impact = price_cost - delay
            if benchmark_price is not None:
                convention = s * (arrival - benchmark_price) * q
    unfilled = max(ordered_quantity - q, 0.0)
    if unfilled <= 1e-12:
        opportunity: float | None = 0.0
    elif post_close_price is not None:
        opportunity = s * (post_close_price - decision_price) * unfilled
    else:
        opportunity = None
    return Shortfall(
        side=side,
        ordered_quantity=ordered_quantity,
        filled_quantity=q,
        decision_price=decision_price,
        arrival_price=arrival,
        fill_price=fill_price,
        benchmark_price=benchmark_price,
        post_close_price=post_close_price,
        expected_bps=expected_bps,
        delay_cost=delay,
        impact_cost=impact,
        fee_cost=fees,
        is_cost=is_cost,
        opportunity_cost=opportunity,
        convention_cost=convention,
    )


def expected_cost_bps(model: CostModel, trade: Trade) -> float | None:
    """What ``model`` expects ``trade`` to cost, in bps of its notional at
    ``trade.price``: the adverse price shift plus the fee."""
    if trade.price <= 0 or trade.quantity <= 0:
        return None
    cost = model.cost(trade)
    shift = _sign(trade.side) * (cost.fill_price - trade.price) / trade.price
    fee = cost.fee / (trade.quantity * trade.price)
    value = (shift + fee) * _BPS
    return value if math.isfinite(value) else None


# ---- decision context ---------------------------------------------------------------


def annotate_orders(
    orders: Sequence[Order],
    *,
    decided_at: datetime,
    prices: Mapping[str, float],
    signals: Mapping[str, Mapping[str, float]],
    cost_model: CostModel | None = None,
    constructor: str | None = None,
    exit_only: bool = False,
    target_weights: Mapping[str, float] | None = None,
    volumes: Mapping[str, float] | None = None,
    asset_classes: Mapping[str, str] | None = None,
) -> list[Order]:
    """``orders`` with their decision price, time, context and expected cost.
    Orders that already carry a decision keep it."""
    out: list[Order] = []
    for order in orders:
        if order.decision_price is not None:
            out.append(order)
            continue
        price = prices.get(order.ticker)
        decision_price = float(price) if price is not None and price > 0 else None
        context = _context(order, signals, constructor, exit_only, target_weights)
        expected = None
        if cost_model is not None and decision_price is not None:
            volume = (volumes or {}).get(order.ticker)
            expected = expected_cost_bps(
                cost_model,
                Trade(
                    ticker=order.ticker,
                    side=order.side,
                    quantity=order.quantity,
                    price=decision_price,
                    asset_class=(asset_classes or {}).get(order.ticker, "equity"),  # type: ignore[arg-type]
                    bar_volume=volume,
                ),
            )
        out.append(
            replace(
                order,
                decision_price=decision_price,
                decided_at=decided_at,
                decision_context=context,
                expected_cost_bps=expected,
            )
        )
    return out


def _context(
    order: Order,
    signals: Mapping[str, Mapping[str, float]],
    constructor: str | None,
    exit_only: bool,
    target_weights: Mapping[str, float] | None,
) -> dict[str, Any]:
    trigger: DecisionTrigger
    if order.strategy_id is None:
        trigger = "risk_rule"
    elif exit_only:
        trigger = "exit_no_pick"
    else:
        trigger = "signal"
    context: dict[str, Any] = {"trigger": trigger, "strategy_id": order.strategy_id}
    scores = signals.get(order.strategy_id or "") or {}
    if order.ticker in scores:
        score = float(scores[order.ticker])
        ranked = sorted(scores.values(), reverse=True)
        context["score"] = _finite(score)
        context["rank"] = 1 + sum(1 for v in ranked if v > score)
        context["candidates"] = len(ranked)
    if constructor is not None:
        context["constructor"] = constructor
    if target_weights is not None and order.ticker in target_weights:
        context["target_weight"] = _finite(float(target_weights[order.ticker]))
    return context


def _finite(value: float) -> float | None:
    return value if math.isfinite(value) else None


# ---- rows and summaries -------------------------------------------------------------


@dataclass(frozen=True)
class OrderTca:
    """One order's identity and shortfall."""

    client_id: str
    portfolio_id: str | None
    strategy_id: str | None
    ticker: str
    side: OrderSide
    status: str
    decided_at: datetime | None
    shortfall: Shortfall
    context: Mapping[str, Any] | None = None


@dataclass
class _Sum:
    cost: float = 0.0
    notional: float = 0.0
    n: int = 0

    def add(self, cost: float | None, notional: float) -> None:
        if cost is None or notional <= 0:
            return
        self.cost += cost
        self.notional += notional
        self.n += 1

    @property
    def bps(self) -> float | None:
        return self.cost / self.notional * _BPS if self.notional > 0 else None


@dataclass(frozen=True)
class TcaGroup:
    """Aggregated costs of a group of orders, in bps of notional."""

    key: str
    orders: int
    filled_orders: int
    #: Filled quantity valued at the decision prices.
    filled_notional: float
    is_cost: float
    is_bps: float | None
    delay_bps: float | None
    impact_bps: float | None
    fee_bps: float | None
    opportunity_cost: float
    opportunity_bps: float | None
    convention_bps: float | None
    #: The cost model's estimate over the orders that recorded one.
    expected_bps: float | None
    #: Realised shortfall minus the estimate, over the same orders.
    model_gap_bps: float | None

    def as_dict(self) -> dict[str, Any]:
        return dict(self.__dict__)


def group_key(row: OrderTca, by: GroupBy) -> str:
    if by == "all":
        return "all"
    if by == "strategy":
        return row.strategy_id or "(none)"
    if by == "ticker":
        return row.ticker
    if by == "portfolio":
        return row.portfolio_id or "(none)"
    day = row.decided_at.date() if row.decided_at is not None else None
    if day is None:
        return "(unknown)"
    if by == "day":
        return day.isoformat()
    if by == "week":
        year, week, _ = day.isocalendar()
        return f"{year}-W{week:02d}"
    return f"{day.year}-{day.month:02d}"


def summarize(rows: Iterable[OrderTca], by: GroupBy = "all") -> list[TcaGroup]:
    """Costs per group, sorted by key."""
    if by not in GROUP_BYS:
        raise ValueError(f"unknown grouping {by!r}; one of {', '.join(GROUP_BYS)}")
    groups: dict[str, list[OrderTca]] = {}
    for row in rows:
        groups.setdefault(group_key(row, by), []).append(row)
    return [_aggregate(key, groups[key]) for key in sorted(groups)]


def _aggregate(key: str, rows: Sequence[OrderTca]) -> TcaGroup:
    is_, delay, impact, fee, conv, opp = _Sum(), _Sum(), _Sum(), _Sum(), _Sum(), _Sum()
    expected, realised = _Sum(), _Sum()
    for row in rows:
        s = row.shortfall
        n = s.filled_notional
        is_.add(s.is_cost, n)
        delay.add(s.delay_cost, n)
        impact.add(s.impact_cost, n)
        fee.add(s.fee_cost if s.filled_quantity > 0 else None, n)
        conv.add(s.convention_cost, n)
        opp.add(s.opportunity_cost, s.unfilled_quantity * s.decision_price)
        if s.expected_bps is not None and s.is_cost is not None:
            expected.add(s.expected_bps * n / _BPS, n)
            realised.add(s.is_cost, n)
    exp_bps, real_bps = expected.bps, realised.bps
    return TcaGroup(
        key=key,
        orders=len(rows),
        filled_orders=sum(1 for r in rows if r.shortfall.filled_quantity > 0),
        filled_notional=is_.notional,
        is_cost=is_.cost,
        is_bps=is_.bps,
        delay_bps=delay.bps,
        impact_bps=impact.bps,
        fee_bps=fee.bps,
        opportunity_cost=opp.cost,
        opportunity_bps=opp.bps,
        convention_bps=conv.bps,
        expected_bps=exp_bps,
        model_gap_bps=(real_bps - exp_bps)
        if exp_bps is not None and real_bps is not None
        else None,
    )


def cost_comparison(
    state: SqliteState,
    strategy_id: str,
    portfolio_id: str | None,
    *,
    since: date | None = None,
) -> dict[str, Any]:
    """Live shortfall of a strategy's orders next to the cost model's
    estimate, for the go-live report (P22): ``orders``, ``live_is_bps``,
    ``modelled_bps`` and ``model_gap_bps`` (``None`` without orders)."""
    groups = summarize(
        load_order_tca(state, portfolio_id, since=since, strategy_id=strategy_id), "all"
    )
    if not groups:
        return {"orders": 0, "live_is_bps": None, "modelled_bps": None, "model_gap_bps": None}
    [g] = groups
    return {
        "orders": g.orders,
        "live_is_bps": g.is_bps,
        "modelled_bps": g.expected_bps,
        "model_gap_bps": g.model_gap_bps,
    }


# ---- backtests ----------------------------------------------------------------------


def backtest_shortfalls(
    fills: Iterable[Fill],
    decision_prices: Mapping[str, float | None],
    reference_price: Callable[[str], float | None],
) -> list[OrderTca]:
    """Shortfall per backtest order from its simulated fills, priced by
    :func:`compute_shortfall` exactly as live orders are. A carried
    remainder (``<root>~<n>``) counts toward its root order.
    ``decision_prices``: the close each order was decided at (see
    ``Backtester.decision_prices``); ``reference_price``: the broker's
    pre-cost price per client id (the arrival: the bar's open, or the
    limit / stop price). Orders without a decision price are skipped."""
    legs: dict[str, list[FillLeg]] = {}
    first: dict[str, Fill] = {}
    for fill in fills:
        root = fill.order_client_id.split("~", 1)[0]
        legs.setdefault(root, []).append(
            FillLeg(fill.quantity, fill.price, fill.fee, reference_price(fill.order_client_id))
        )
        first.setdefault(root, fill)
    rows: list[OrderTca] = []
    for root, order_legs in legs.items():
        decision = decision_prices.get(root)
        if decision is None or decision <= 0:
            continue
        fill = first[root]
        shortfall = compute_shortfall(
            fill.side, decision, sum(leg.quantity for leg in order_legs), order_legs
        )
        rows.append(
            OrderTca(
                client_id=root,
                portfolio_id=None,
                strategy_id=None,
                ticker=fill.ticker,
                side=fill.side,
                status="filled",
                decided_at=None,
                shortfall=shortfall,
            )
        )
    return rows


# ---- ledger reads and writes -------------------------------------------------------


def tca_recorded(state: SqliteState) -> bool:
    """The ledger carries the TCA columns (migration 017)."""
    return "decision_price" in ledger_columns(state, "orders")


def decision_values(order: Order) -> dict[str, Any]:
    """The ``orders`` columns that record ``order``'s decision."""
    return {
        "decision_price": order.decision_price,
        "decided_at": _iso(order.decided_at) if order.decided_at is not None else None,
        "decision_context_json": (
            json.dumps(dict(order.decision_context), sort_keys=True, default=str)
            if order.decision_context is not None
            else None
        ),
        "expected_cost_bps": order.expected_cost_bps,
    }


def load_order_tca(
    state: SqliteState,
    portfolio_id: str | None,
    *,
    since: date | None = None,
    until: date | None = None,
    strategy_id: str | None = None,
    ticker: str | None = None,
    client_id: str | None = None,
) -> list[OrderTca]:
    """Shortfall of every order of ``portfolio_id`` (``None``: every
    portfolio, admin reads only) that recorded a decision price, oldest
    first."""
    if not tca_recorded(state):
        return []
    where, params = ledger_filter(state, "orders", portfolio_id)
    clauses = [where, "decision_price IS NOT NULL", "decision_price > 0"]
    if since is not None:
        clauses.append("decided_at >= ?")
        params.append(since.isoformat())
    if until is not None:
        clauses.append("substr(decided_at, 1, 10) <= ?")
        params.append(until.isoformat())
    for col, value in (("strategy_id", strategy_id), ("ticker", ticker), ("client_id", client_id)):
        if value is not None:
            clauses.append(f"{col} = ?")
            params.append(value)
    orders = state.sql(
        f"SELECT * FROM orders WHERE {' AND '.join(clauses)} ORDER BY decided_at, client_id",
        params,
    )
    if not orders:
        return []
    fills = _fills_by_order(state, [r["client_id"] for r in orders])
    return [_order_tca(dict(r), fills.get(r["client_id"], [])) for r in orders]


def _fills_by_order(state: SqliteState, client_ids: Sequence[str]) -> dict[str, list[FillLeg]]:
    out: dict[str, list[FillLeg]] = {}
    for start in range(0, len(client_ids), 500):
        chunk = list(client_ids[start : start + 500])
        marks = ",".join("?" for _ in chunk)
        rows = state.sql(
            "SELECT order_client_id, quantity, price, fee, arrival_price FROM fills"
            f" WHERE order_client_id IN ({marks}) ORDER BY id",
            chunk,
        )
        for r in rows:
            out.setdefault(r["order_client_id"], []).append(
                FillLeg(
                    quantity=float(r["quantity"]),
                    price=float(r["price"]),
                    fee=float(r["fee"] or 0.0),
                    arrival_price=_float(r["arrival_price"]),
                )
            )
    return out


def _order_tca(row: Mapping[str, Any], legs: Sequence[FillLeg]) -> OrderTca:
    shortfall = compute_shortfall(
        row["side"],
        float(row["decision_price"]),
        float(row["quantity"]),
        legs,
        benchmark_price=_float(row.get("benchmark_price")),
        post_close_price=_float(row.get("post_close_price")) if _may_miss(row) else None,
        expected_bps=_float(row.get("expected_cost_bps")),
    )
    return OrderTca(
        client_id=row["client_id"],
        portfolio_id=row.get("portfolio_id"),
        strategy_id=row.get("strategy_id"),
        ticker=row["ticker"],
        side=row["side"],
        status=row["status"],
        decided_at=_parse_ts(row.get("decided_at")),
        shortfall=shortfall,
        context=_context_of(row.get("decision_context_json")),
    )


def _may_miss(row: Mapping[str, Any]) -> bool:
    """An order still working has no opportunity cost yet."""
    return row["status"] not in ("pending", "partially_filled")


def _context_of(raw: str | None) -> dict[str, Any] | None:
    if not raw:
        return None
    try:
        value = json.loads(raw)
    except json.JSONDecodeError:
        return None
    return value if isinstance(value, dict) else None


def refresh_benchmarks(
    state: SqliteState, lake: DuckDBLake, *, portfolio_id: str | None = None
) -> int:
    """Fill ``benchmark_price`` (the next session's open) and
    ``post_close_price`` (its close) of orders decided before the latest
    ingested bar, and the unknown arrival prices of their fills with the
    next open (an external broker's market orders arrive at the open).
    Returns the number of orders updated; idempotent."""
    if not tca_recorded(state):
        return 0
    where, params = ledger_filter(state, "orders", portfolio_id)
    rows = state.sql(
        "SELECT client_id, ticker, substr(decided_at, 1, 10) AS day FROM orders"
        f" WHERE {where} AND decided_at IS NOT NULL"
        " AND (benchmark_price IS NULL OR post_close_price IS NULL)",
        params,
    )
    by_day: dict[str, dict[str, list[str]]] = {}
    for r in rows:
        by_day.setdefault(r["day"], {}).setdefault(r["ticker"], []).append(r["client_id"])
    updated = 0
    for day, tickers in sorted(by_day.items()):
        nexts = _next_session(lake, sorted(tickers), date.fromisoformat(day))
        if not nexts:
            continue
        with state.transaction():
            for ticker, (open_, close) in nexts.items():
                for client_id in tickers[ticker]:
                    cur = state.execute(
                        "UPDATE orders SET benchmark_price = COALESCE(benchmark_price, ?),"
                        " post_close_price = COALESCE(post_close_price, ?) WHERE client_id = ?",
                        [open_, close, client_id],
                    )
                    updated += cur.rowcount
                    if open_ is not None:
                        state.execute(
                            "UPDATE fills SET arrival_price = ?"
                            " WHERE order_client_id = ? AND arrival_price IS NULL",
                            [open_, client_id],
                        )
    if updated:
        _log.info("tca.benchmarks_refreshed", orders=updated)
    return updated


def _next_session(
    lake: DuckDBLake, tickers: Sequence[str], day: date
) -> dict[str, tuple[float | None, float | None]]:
    """``(open, close)`` of each ticker's first daily bar after ``day``."""
    df = lake.sql(
        """
        SELECT ticker, arg_min(open, date) AS open, arg_min(close, date) AS close
          FROM prices
         WHERE ticker = ANY(?) AND date > ?
         GROUP BY ticker
        """,
        [list(tickers), day],
    )
    out: dict[str, tuple[float | None, float | None]] = {}
    for row in df.itertuples(index=False):
        open_, close = _positive(row.open), _positive(row.close)
        if open_ is not None or close is not None:
            out[str(row.ticker)] = (open_, close)
    return out


# ---- journal -----------------------------------------------------------------------


class JournalError(LookupError):
    """The order or note is not in the caller's portfolio (or not theirs)."""


@dataclass(frozen=True)
class JournalNote:
    id: int
    order_client_id: str
    author: str
    note: str
    created_at: str
    updated_at: str


@dataclass(frozen=True)
class JournalEntry:
    """One order in the journal: its reason, the signal context, the
    outcome and the notes people added."""

    client_id: str
    portfolio_id: str | None
    strategy_id: str | None
    ticker: str
    side: OrderSide
    quantity: float
    status: str
    status_reason: str | None
    created_at: str
    decided_at: str | None
    decision_price: float | None
    context: Mapping[str, Any] | None
    shortfall: Shortfall | None
    notes: tuple[JournalNote, ...]

    @property
    def trigger(self) -> str | None:
        return (self.context or {}).get("trigger")

    @property
    def next_session_move_bps(self) -> float | None:
        """How the price moved in the trade's favour from the decision to
        the next session's close, in bps (positive: the decision was right
        for the day)."""
        s = self.shortfall
        if s is None or s.post_close_price is None:
            return None
        return _sign(s.side) * (s.post_close_price / s.decision_price - 1.0) * _BPS


def journal(
    state: SqliteState,
    portfolio_id: str | None,
    *,
    since: date | None = None,
    strategy_id: str | None = None,
    ticker: str | None = None,
    client_id: str | None = None,
    limit: int = 50,
    offset: int = 0,
) -> tuple[list[JournalEntry], int]:
    """A page of the journal, newest first, and the total count."""
    where, params = ledger_filter(state, "orders", portfolio_id)
    clauses = [where]
    recorded = tca_recorded(state)
    if since is not None:
        clauses.append("COALESCE(decided_at, created_at) >= ?" if recorded else "created_at >= ?")
        params.append(since.isoformat())
    for col, value in (("strategy_id", strategy_id), ("ticker", ticker), ("client_id", client_id)):
        if value is not None:
            clauses.append(f"{col} = ?")
            params.append(value)
    predicate = " AND ".join(clauses)
    total = int(state.sql(f"SELECT COUNT(*) FROM orders WHERE {predicate}", params)[0][0])
    rows = state.sql(
        f"SELECT * FROM orders WHERE {predicate} ORDER BY created_at DESC, rowid DESC"
        " LIMIT ? OFFSET ?",
        [*params, limit, offset],
    )
    ids = [r["client_id"] for r in rows]
    fills = _fills_by_order(state, ids) if ids else {}
    notes = _notes_by_order(state, ids) if ids and recorded else {}
    entries = []
    for r in rows:
        row = dict(r)
        decision = _float(row.get("decision_price"))
        shortfall = (
            _order_tca(row, fills.get(row["client_id"], [])).shortfall
            if decision is not None and decision > 0
            else None
        )
        entries.append(
            JournalEntry(
                client_id=row["client_id"],
                portfolio_id=row.get("portfolio_id"),
                strategy_id=row.get("strategy_id"),
                ticker=row["ticker"],
                side=row["side"],
                quantity=float(row["quantity"]),
                status=row["status"],
                status_reason=row.get("status_reason"),
                created_at=row["created_at"],
                decided_at=row.get("decided_at"),
                decision_price=decision,
                context=_context_of(row.get("decision_context_json")),
                shortfall=shortfall,
                notes=tuple(notes.get(row["client_id"], [])),
            )
        )
    return entries, total


def _notes_by_order(state: SqliteState, client_ids: Sequence[str]) -> dict[str, list[JournalNote]]:
    out: dict[str, list[JournalNote]] = {}
    for start in range(0, len(client_ids), 500):
        chunk = list(client_ids[start : start + 500])
        marks = ",".join("?" for _ in chunk)
        for r in state.sql(
            f"SELECT * FROM journal_notes WHERE order_client_id IN ({marks}) ORDER BY id", chunk
        ):
            out.setdefault(r["order_client_id"], []).append(_note(r))
    return out


def _note(row: Mapping[str, Any]) -> JournalNote:
    return JournalNote(
        id=int(row["id"]),
        order_client_id=row["order_client_id"],
        author=row["author"],
        note=row["note"],
        created_at=row["created_at"],
        updated_at=row["updated_at"],
    )


def add_note(
    state: SqliteState,
    client_id: str,
    portfolio_id: str,
    *,
    author: str,
    note: str,
    now: datetime | None = None,
) -> JournalNote:
    """Add a note to an order of ``portfolio_id`` (:class:`JournalError`
    when the order is not there)."""
    text = _clean(note)
    where, params = ledger_filter(state, "orders", portfolio_id)
    found = state.sql(
        f"SELECT portfolio_id FROM orders WHERE client_id = ? AND {where}", [client_id, *params]
    )
    if not found:
        raise JournalError(f"order {client_id!r} not found")
    stamp = _iso(now or datetime.now(UTC))
    with state.transaction():
        cur = state.execute(
            "INSERT INTO journal_notes (order_client_id, portfolio_id, author, note,"
            " created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?)",
            [client_id, found[0]["portfolio_id"], author, text, stamp, stamp],
        )
    return get_note(state, int(cur.lastrowid), portfolio_id)


def get_note(state: SqliteState, note_id: int, portfolio_id: str | None) -> JournalNote:
    params: list[Any] = [note_id]
    sql = "SELECT * FROM journal_notes WHERE id = ?"
    if portfolio_id is not None:
        sql += " AND portfolio_id = ?"
        params.append(portfolio_id)
    rows = state.sql(sql, params)
    if not rows:
        raise JournalError(f"note {note_id} not found")
    return _note(rows[0])


def update_note(
    state: SqliteState,
    note_id: int,
    portfolio_id: str,
    *,
    author: str,
    note: str,
    now: datetime | None = None,
) -> JournalNote:
    """Replace a note's text. Only its author may (anyone else reads it as
    missing)."""
    text = _clean(note)
    current = get_note(state, note_id, portfolio_id)
    if current.author != author:
        raise JournalError(f"note {note_id} not found")
    with state.transaction():
        state.execute(
            "UPDATE journal_notes SET note = ?, updated_at = ? WHERE id = ?",
            [text, _iso(now or datetime.now(UTC)), note_id],
        )
    return get_note(state, note_id, portfolio_id)


def _clean(note: str) -> str:
    text = note.strip()
    if not text:
        raise ValueError("a note needs some text")
    return text


# ---- helpers -----------------------------------------------------------------------


def _iso(ts: datetime) -> str:
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=UTC)
    return ts.isoformat(timespec="seconds")


def _parse_ts(raw: str | None) -> datetime | None:
    if not raw:
        return None
    try:
        ts = datetime.fromisoformat(raw)
    except ValueError:
        return None
    return ts if ts.tzinfo is not None else ts.replace(tzinfo=UTC)


def _float(value: Any) -> float | None:
    if value is None:
        return None
    try:
        out = float(value)
    except (TypeError, ValueError):
        return None
    return out if math.isfinite(out) else None


def _positive(value: Any) -> float | None:
    out = _float(value)
    return out if out is not None and out > 0 else None

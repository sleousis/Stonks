"""Transaction cost analysis for minute trading (roadmap 21.3.5, P21, P22).

The daily TCA (:mod:`stonks.production.tca`) prices an order against the
next session. An intraday order lives for minutes, so this module prices it
against minutes and against the quotes the stream recorded
(:mod:`stonks.streaming.recorder`):

- **Arrival** is the open of the next minute bar: the first bar that starts
  at or after the decision (a decision on the close of minute ``t`` arrives
  at the open of ``t+1``, the price a backtest fills at, P21). Without that
  bar the fills' recorded arrival price, then the quote mid at that minute,
  stand in. ``arrival_source`` says which.
- **Spread** comes from the recorded quotes: the last valid quote at or
  before the decision and at or before each fill, never a later one, and
  never one older than ``max_quote_age``.
- **Shortfall** uses the same :func:`~stonks.production.tca.compute_shortfall`
  as the daily path, so the numbers are comparable. The impact part splits
  into the quoted half spread paid (``spread_cost``) and what is left
  (``residual_impact``: slippage and market impact beyond the spread). The
  opportunity cost of an unfilled part is measured at the day's last minute
  close. The convention gap does not apply (arrival is already the next
  minute) and stays ``None``.
- **Summaries** group by order, strategy, strategy sleeve (portfolio and
  strategy), ticker, portfolio or day.
- **Calibration** turns the same rows into evidence for
  :func:`stonks.backtest.cost_calibration.fit_minute_costs`.

Point in time: :class:`MinuteBars` and :class:`QuoteBook` take an ``end``
and never read past it, and :func:`load_intraday_tca` drops orders decided
and fills booked after ``until``. A calibration to day ``D`` uses nothing
after the end of ``D``.

An order counts as intraday when its decision context names an intraday
``interval``. Without one, an order placed outside a tick (no ``tick_id``)
and not by a person counts as intraday, which is how the engine records
them (21.2.3).
"""

from __future__ import annotations

import bisect
import json
import math
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal

import pandas as pd

from stonks.backtest.cost_calibration import CostCalibration, FillSample, fit_minute_costs
from stonks.backtest.costs import CostModelSettings
from stonks.core.interval import Interval
from stonks.core.stream import QuoteTick
from stonks.core.types import AssetClass, OrderSide
from stonks.production.ledger import ledger_filter
from stonks.production.tca import (
    FillLeg,
    OrderTca,
    TcaGroup,
    compute_shortfall,
    summarize,
    tca_recorded,
)

if TYPE_CHECKING:
    from stonks.store.lake import DuckDBLake
    from stonks.store.state import SqliteState

_BPS = 10_000.0
MINUTE = Interval.parse("1m")
#: A quote older than this is not the market at that moment.
DEFAULT_MAX_QUOTE_AGE = timedelta(seconds=60)

ArrivalSource = Literal["next_bar", "fill", "quote"]
IntradayGroupBy = Literal["all", "strategy", "sleeve", "ticker", "portfolio", "day"]
INTRADAY_GROUP_BYS: tuple[IntradayGroupBy, ...] = (
    "all",
    "strategy",
    "sleeve",
    "ticker",
    "portfolio",
    "day",
)

#: ``(ticker, start, end) -> bars`` with ``timestamp`` (naive UTC bar start),
#: ``open``, ``close`` and ``volume``, both bounds inclusive.
BarReader = Callable[[str, datetime, datetime], pd.DataFrame]


def _utc(when: datetime) -> datetime:
    return when.replace(tzinfo=UTC) if when.tzinfo is None else when.astimezone(UTC)


def _floor(when: datetime, step: Interval) -> datetime:
    ts = _utc(when).timestamp()
    return datetime.fromtimestamp((ts // step.seconds) * step.seconds, UTC)


def _ceil(when: datetime, step: Interval) -> datetime:
    floor = _floor(when, step)
    return floor if floor == _utc(when) else floor + step.to_timedelta()


# ---- recorded quotes --------------------------------------------------------------


@dataclass(frozen=True)
class QuoteSnapshot:
    """One recorded quote."""

    ticker: str
    timestamp: datetime
    bid: float
    ask: float

    @property
    def mid(self) -> float:
        return (self.bid + self.ask) / 2.0

    @property
    def spread_bps(self) -> float:
        return (self.ask - self.bid) / self.mid * _BPS

    @property
    def half_spread_bps(self) -> float:
        return self.spread_bps / 2.0

    @property
    def half_spread(self) -> float:
        """Half the spread, in price."""
        return (self.ask - self.bid) / 2.0


class QuoteBook:
    """Recorded quotes per ticker, read as of a moment. Quotes without both
    sides, with a non-positive bid or crossed are left out."""

    def __init__(self, quotes: Iterable[QuoteTick]) -> None:
        per: dict[str, list[QuoteSnapshot]] = {}
        for q in quotes:
            if q.bid is None or q.ask is None or q.bid <= 0 or q.ask < q.bid:
                continue
            snap = QuoteSnapshot(q.ticker, _utc(q.timestamp), float(q.bid), float(q.ask))
            per.setdefault(q.ticker, []).append(snap)
        self._quotes = {t: sorted(v, key=lambda s: s.timestamp) for t, v in per.items()}
        self._times = {t: [s.timestamp for s in v] for t, v in self._quotes.items()}

    @classmethod
    def from_recording(
        cls,
        root: str | Path,
        *,
        tickers: Sequence[str] | None = None,
        start: datetime | None = None,
        end: datetime | None = None,
    ) -> QuoteBook:
        """The quotes of a :class:`~stonks.streaming.recorder.StreamRecorder`
        recording, bounded by ``start`` and ``end`` (both inclusive)."""
        from stonks.streaming.recorder import read_recording

        events = read_recording(root, tickers=tickers, start=start, end=end)
        return cls(e for e in events if isinstance(e, QuoteTick))

    @property
    def tickers(self) -> list[str]:
        return sorted(self._quotes)

    def count(self) -> int:
        return sum(len(v) for v in self._quotes.values())

    def at(
        self, ticker: str, when: datetime, *, max_age: timedelta | None = DEFAULT_MAX_QUOTE_AGE
    ) -> QuoteSnapshot | None:
        """The last quote at or before ``when``, or ``None`` (none yet, or
        older than ``max_age``)."""
        times = self._times.get(ticker)
        if not times:
            return None
        when = _utc(when)
        i = bisect.bisect_right(times, when) - 1
        if i < 0:
            return None
        snap = self._quotes[ticker][i]
        if max_age is not None and when - snap.timestamp > max_age:
            return None
        return snap

    def half_spreads(
        self, start: datetime | None = None, end: datetime | None = None
    ) -> dict[str, list[float]]:
        """Every quote's half spread in bps, per ticker, inside the bounds."""
        lo = _utc(start) if start is not None else None
        hi = _utc(end) if end is not None else None
        out: dict[str, list[float]] = {}
        for ticker, snaps in self._quotes.items():
            values = [
                s.half_spread_bps
                for s in snaps
                if (lo is None or s.timestamp >= lo) and (hi is None or s.timestamp <= hi)
            ]
            if values:
                out[ticker] = values
        return out


# ---- minute bars ------------------------------------------------------------------


class MinuteBars:
    """Intraday bars per ticker and UTC day, read through ``read`` and never
    past ``end``."""

    def __init__(
        self, read: BarReader, *, interval: Interval = MINUTE, end: datetime | None = None
    ) -> None:
        if not interval.is_intraday:
            raise ValueError(f"intraday TCA needs an intraday interval, got {interval.code}")
        self._read = read
        self.interval = interval
        self.end = _utc(end) if end is not None else None
        self._cache: dict[tuple[str, date], list[tuple[datetime, float, float, float | None]]] = {}

    def _day(self, ticker: str, day: date) -> list[tuple[datetime, float, float, float | None]]:
        key = (ticker, day)
        if key in self._cache:
            return self._cache[key]
        start = datetime.combine(day, time.min, UTC)
        stop = datetime.combine(day, time.max, UTC)
        if self.end is not None:
            stop = min(stop, self.end)
        rows: list[tuple[datetime, float, float, float | None]] = []
        if stop >= start:
            frame = self._read(ticker, start, stop)
            for r in frame.to_dict("records"):
                when = pd.Timestamp(r["timestamp"]).to_pydatetime()
                if not isinstance(when, datetime):  # NaT
                    continue
                ts = _utc(when)
                if self.end is not None and ts > self.end:
                    continue
                raw = r.get("volume")
                volume = None if raw is None or pd.isna(raw) else float(raw)
                rows.append((ts, float(r["open"]), float(r["close"]), volume))
        rows.sort(key=lambda row: row[0])
        self._cache[key] = rows
        return rows

    def next_open(self, ticker: str, when: datetime) -> tuple[datetime, float] | None:
        """``(start, open)`` of the first bar starting at or after ``when``
        on the same UTC day."""
        target = _ceil(when, self.interval)
        for ts, open_, _, _ in self._day(ticker, _utc(when).date()):
            if ts >= target and open_ > 0:
                return ts, open_
        return None

    def volume_at(self, ticker: str, when: datetime) -> float | None:
        """The volume of the bar that holds ``when``."""
        start = _floor(when, self.interval)
        for ts, _, _, volume in self._day(ticker, start.date()):
            if ts == start:
                return volume
        return None

    def session_close(self, ticker: str, when: datetime) -> float | None:
        """The close of the day's last bar at or after ``when``'s bar."""
        start = _floor(when, self.interval)
        closes = [c for ts, _, c, _ in self._day(ticker, start.date()) if ts >= start and c > 0]
        return closes[-1] if closes else None


def lake_bar_reader(lake: DuckDBLake, interval: Interval = MINUTE) -> BarReader:
    """A :data:`BarReader` over the lake's ``bars``."""

    def read(ticker: str, start: datetime, end: datetime) -> pd.DataFrame:
        naive_start = _utc(start).replace(tzinfo=None)
        naive_end = _utc(end).replace(tzinfo=None)
        return lake.get_bars(ticker, interval, naive_start, naive_end)

    return read


# ---- orders -----------------------------------------------------------------------


def _context(raw: Any) -> dict[str, Any]:
    if not raw:
        return {}
    try:
        value = json.loads(raw)
    except (TypeError, json.JSONDecodeError):
        return {}
    return value if isinstance(value, dict) else {}


def is_intraday_order(row: Mapping[str, Any]) -> bool:
    """Whether an ``orders`` row is an intraday order (see the module doc)."""
    code = _context(row.get("decision_context_json")).get("interval")
    if code:
        try:
            return Interval.parse(str(code)).is_intraday
        except ValueError:
            return False
    return row.get("tick_id") is None and row.get("origin") != "manual"


@dataclass(frozen=True)
class IntradayLeg:
    """One fill of an intraday order, with the market around it."""

    quantity: float
    price: float
    fee: float
    filled_at: datetime | None
    #: Volume of the minute the fill landed in.
    bar_volume: float | None
    quote: QuoteSnapshot | None


@dataclass(frozen=True)
class IntradayOrderTca:
    """One intraday order's shortfall, split with the quoted spread."""

    order: OrderTca
    interval: str
    arrival_source: ArrivalSource | None
    #: Quoted spread at the decision, in bps of the mid.
    decision_spread_bps: float | None
    #: Quoted spread at the fills, weighted by quantity, in bps of the mid.
    fill_spread_bps: float | None
    #: The quoted half spread paid, in currency (``None`` when a fill had no quote).
    spread_cost: float | None
    #: Impact beyond the half spread, in currency.
    residual_impact: float | None
    legs: tuple[IntradayLeg, ...] = ()

    def _bps(self, cost: float | None) -> float | None:
        notional = self.order.shortfall.filled_notional
        return None if cost is None or notional <= 0 else cost / notional * _BPS

    @property
    def spread_cost_bps(self) -> float | None:
        return self._bps(self.spread_cost)

    @property
    def residual_impact_bps(self) -> float | None:
        return self._bps(self.residual_impact)

    def as_dict(self) -> dict[str, Any]:
        o = self.order
        return {
            "client_id": o.client_id,
            "portfolio_id": o.portfolio_id,
            "strategy_id": o.strategy_id,
            "ticker": o.ticker,
            "side": o.side,
            "status": o.status,
            "decided_at": o.decided_at.isoformat() if o.decided_at else None,
            "interval": self.interval,
            "arrival_source": self.arrival_source,
            "decision_spread_bps": self.decision_spread_bps,
            "fill_spread_bps": self.fill_spread_bps,
            "spread_cost_bps": self.spread_cost_bps,
            "residual_impact_bps": self.residual_impact_bps,
            **o.shortfall.as_dict(),
        }


def _float(value: Any) -> float | None:
    if value is None:
        return None
    try:
        out = float(value)
    except (TypeError, ValueError):
        return None
    return out if math.isfinite(out) else None


def _parse(raw: Any) -> datetime | None:
    if not raw:
        return None
    try:
        return _utc(datetime.fromisoformat(str(raw)))
    except ValueError:
        return None


def _sign(side: OrderSide) -> float:
    return 1.0 if side == "buy" else -1.0


def load_intraday_tca(
    state: SqliteState,
    quotes: QuoteBook,
    bars: MinuteBars,
    portfolio_id: str | None,
    *,
    since: datetime | None = None,
    until: datetime | None = None,
    strategy_id: str | None = None,
    ticker: str | None = None,
    max_quote_age: timedelta | None = DEFAULT_MAX_QUOTE_AGE,
) -> list[IntradayOrderTca]:
    """Intraday orders of ``portfolio_id`` (``None``: every portfolio)
    decided inside ``[since, until]``, oldest first. Fills after ``until``
    are not seen."""
    if not tca_recorded(state):
        return []
    lo = _utc(since) if since is not None else None
    hi = _utc(until) if until is not None else None
    where, params = ledger_filter(state, "orders", portfolio_id)
    clauses = [where, "decision_price IS NOT NULL", "decision_price > 0", "decided_at IS NOT NULL"]
    if lo is not None:
        clauses.append("substr(decided_at, 1, 10) >= ?")
        params.append((lo - timedelta(days=1)).date().isoformat())
    if hi is not None:
        clauses.append("substr(decided_at, 1, 10) <= ?")
        params.append((hi + timedelta(days=1)).date().isoformat())
    for col, value in (("strategy_id", strategy_id), ("ticker", ticker)):
        if value is not None:
            clauses.append(f"{col} = ?")
            params.append(value)
    rows = state.sql(
        f"SELECT * FROM orders WHERE {' AND '.join(clauses)} ORDER BY decided_at, client_id",
        params,
    )
    picked: list[tuple[dict[str, Any], datetime]] = []
    for r in rows:
        row = dict(r)
        decided = _parse(row.get("decided_at"))
        if decided is None or not is_intraday_order(row):
            continue
        if (lo is not None and decided < lo) or (hi is not None and decided > hi):
            continue
        picked.append((row, decided))
    fills = _fills(state, [row["client_id"] for row, _ in picked], hi)
    return [
        _price_order(row, decided, fills.get(row["client_id"], []), quotes, bars, max_quote_age)
        for row, decided in picked
    ]


def _fills(
    state: SqliteState, client_ids: Sequence[str], until: datetime | None
) -> dict[str, list[dict[str, Any]]]:
    out: dict[str, list[dict[str, Any]]] = {}
    for start in range(0, len(client_ids), 500):
        chunk = list(client_ids[start : start + 500])
        marks = ",".join("?" for _ in chunk)
        for r in state.sql(
            "SELECT order_client_id, quantity, price, fee, filled_at, arrival_price FROM fills"
            f" WHERE order_client_id IN ({marks}) ORDER BY id",
            chunk,
        ):
            row = dict(r)
            at = _parse(row["filled_at"])
            if until is not None and (at is None or at > until):
                continue
            row["filled_at"] = at
            out.setdefault(row["order_client_id"], []).append(row)
    return out


def _price_order(
    row: Mapping[str, Any],
    decided: datetime,
    fills: Sequence[Mapping[str, Any]],
    quotes: QuoteBook,
    bars: MinuteBars,
    max_age: timedelta | None,
) -> IntradayOrderTca:
    ticker, side = row["ticker"], row["side"]
    arrival, source = _arrival(ticker, decided, fills, quotes, bars, max_age)
    legs = tuple(
        IntradayLeg(
            quantity=float(f["quantity"]),
            price=float(f["price"]),
            fee=float(f["fee"] or 0.0),
            filled_at=f["filled_at"],
            bar_volume=bars.volume_at(ticker, f["filled_at"]) if f["filled_at"] else None,
            quote=quotes.at(ticker, f["filled_at"], max_age=max_age) if f["filled_at"] else None,
        )
        for f in fills
        if float(f["quantity"]) > 0
    )
    working = row["status"] in ("pending", "partially_filled")
    shortfall = compute_shortfall(
        side,
        float(row["decision_price"]),
        float(row["quantity"]),
        [FillLeg(leg.quantity, leg.price, leg.fee, arrival) for leg in legs],
        arrival_price=arrival,
        post_close_price=None if working else bars.session_close(ticker, decided),
        expected_bps=_float(row.get("expected_cost_bps")),
    )
    decision_quote = quotes.at(ticker, decided, max_age=max_age)
    fill_spread = spread_cost = residual = None
    if legs and all(leg.quote is not None for leg in legs):
        q = sum(leg.quantity for leg in legs)
        fill_spread = sum(leg.quantity * leg.quote.spread_bps for leg in legs) / q  # type: ignore[union-attr]
        spread_cost = sum(leg.quantity * leg.quote.half_spread for leg in legs)  # type: ignore[union-attr]
        if shortfall.impact_cost is not None:
            residual = shortfall.impact_cost - spread_cost
    context = _context(row.get("decision_context_json"))
    order = OrderTca(
        client_id=row["client_id"],
        portfolio_id=row.get("portfolio_id"),
        strategy_id=row.get("strategy_id"),
        ticker=ticker,
        side=side,
        status=row["status"],
        decided_at=decided,
        shortfall=shortfall,
        context=context or None,
    )
    return IntradayOrderTca(
        order=order,
        interval=str(context.get("interval") or bars.interval.code),
        arrival_source=source,
        decision_spread_bps=decision_quote.spread_bps if decision_quote else None,
        fill_spread_bps=fill_spread,
        spread_cost=spread_cost,
        residual_impact=residual,
        legs=legs,
    )


def _arrival(
    ticker: str,
    decided: datetime,
    fills: Sequence[Mapping[str, Any]],
    quotes: QuoteBook,
    bars: MinuteBars,
    max_age: timedelta | None,
) -> tuple[float | None, ArrivalSource | None]:
    nxt = bars.next_open(ticker, decided)
    if nxt is not None:
        return nxt[1], "next_bar"
    recorded = [(_float(f["arrival_price"]), float(f["quantity"])) for f in fills]
    if recorded and all(a is not None and a > 0 for a, _ in recorded):
        q = sum(qty for _, qty in recorded)
        if q > 0:
            return sum(a * qty for a, qty in recorded) / q, "fill"  # type: ignore[operator]
    quote = quotes.at(ticker, _ceil(decided, bars.interval), max_age=max_age)
    if quote is not None:
        return quote.mid, "quote"
    return None, None


# ---- summaries --------------------------------------------------------------------


@dataclass(frozen=True)
class IntradayTcaGroup:
    """A group's daily-style costs (:class:`TcaGroup`) plus the spread split."""

    key: str
    base: TcaGroup
    decision_spread_bps: float | None
    fill_spread_bps: float | None
    spread_cost_bps: float | None
    residual_impact_bps: float | None

    def as_dict(self) -> dict[str, Any]:
        return {
            **self.base.as_dict(),
            "key": self.key,
            "decision_spread_bps": self.decision_spread_bps,
            "fill_spread_bps": self.fill_spread_bps,
            "spread_cost_bps": self.spread_cost_bps,
            "residual_impact_bps": self.residual_impact_bps,
        }


def _key(row: IntradayOrderTca, by: IntradayGroupBy) -> str:
    o = row.order
    if by == "all":
        return "all"
    if by == "strategy":
        return o.strategy_id or "(none)"
    if by == "sleeve":
        return f"{o.portfolio_id or '(none)'}/{o.strategy_id or '(none)'}"
    if by == "ticker":
        return o.ticker
    if by == "portfolio":
        return o.portfolio_id or "(none)"
    return o.decided_at.date().isoformat() if o.decided_at else "(unknown)"


def _weighted(pairs: Iterable[tuple[float | None, float]]) -> float | None:
    total = weight = 0.0
    for value, w in pairs:
        if value is None or w <= 0:
            continue
        total += value * w
        weight += w
    return total / weight if weight > 0 else None


def _ratio(pairs: Iterable[tuple[float | None, float]]) -> float | None:
    cost = notional = 0.0
    for value, n in pairs:
        if value is None or n <= 0:
            continue
        cost += value
        notional += n
    return cost / notional * _BPS if notional > 0 else None


def summarize_intraday(
    rows: Iterable[IntradayOrderTca], by: IntradayGroupBy = "all"
) -> list[IntradayTcaGroup]:
    """Costs per group, sorted by key."""
    if by not in INTRADAY_GROUP_BYS:
        raise ValueError(f"unknown grouping {by!r}; one of {', '.join(INTRADAY_GROUP_BYS)}")
    groups: dict[str, list[IntradayOrderTca]] = {}
    for row in rows:
        groups.setdefault(_key(row, by), []).append(row)
    out = []
    for key in sorted(groups):
        members = groups[key]
        [base] = summarize([m.order for m in members], "all")
        out.append(
            IntradayTcaGroup(
                key=key,
                base=base,
                decision_spread_bps=_weighted(
                    (m.decision_spread_bps, m.order.shortfall.decision_notional) for m in members
                ),
                fill_spread_bps=_weighted(
                    (m.fill_spread_bps, m.order.shortfall.filled_notional) for m in members
                ),
                spread_cost_bps=_ratio(
                    (m.spread_cost, m.order.shortfall.filled_notional) for m in members
                ),
                residual_impact_bps=_ratio(
                    (m.residual_impact, m.order.shortfall.filled_notional) for m in members
                ),
            )
        )
    return out


# ---- calibration ------------------------------------------------------------------


def calibration_evidence(
    rows: Iterable[IntradayOrderTca],
    quotes: QuoteBook,
    *,
    start: datetime | None,
    end: datetime,
    asset_classes: Mapping[str, AssetClass],
) -> tuple[dict[AssetClass, list[float]], list[FillSample]]:
    """Quoted half spreads per asset class and one sample per fill, all
    inside ``[start, end]``."""
    spreads: dict[AssetClass, list[float]] = {}
    for ticker, values in quotes.half_spreads(start, end).items():
        spreads.setdefault(asset_classes.get(ticker, "equity"), []).extend(values)
    lo = _utc(start) if start is not None else None
    hi = _utc(end)
    samples: list[FillSample] = []
    for row in rows:
        arrival = row.order.shortfall.arrival_price
        if arrival is None or arrival <= 0:
            continue
        s = _sign(row.order.side)
        for leg in row.legs:
            if leg.filled_at is None or leg.filled_at > hi or (lo and leg.filled_at < lo):
                continue
            samples.append(
                FillSample(
                    asset_class=asset_classes.get(row.order.ticker, "equity"),
                    quantity=leg.quantity,
                    bar_volume=leg.bar_volume,
                    cost_bps=s * (leg.price - arrival) / arrival * _BPS,
                    half_spread_bps=leg.quote.half_spread_bps if leg.quote else None,
                    notional=leg.quantity * leg.price,
                    fee=leg.fee,
                )
            )
    return spreads, samples


def calibrate_minute_costs(
    state: SqliteState,
    recording: str | Path,
    read_bars: BarReader,
    *,
    end: date,
    current: CostModelSettings,
    start: date | None = None,
    interval: Interval = MINUTE,
    asset_classes: Mapping[str, AssetClass] | None = None,
    portfolio_id: str | None = None,
    max_quote_age: timedelta | None = DEFAULT_MAX_QUOTE_AGE,
    min_quotes: int | None = None,
    min_fills: int | None = None,
) -> CostCalibration:
    """Fit the cost model's half spreads and impact to the intraday orders,
    fills and recorded quotes up to the end of ``end`` (never later)."""
    if not interval.is_intraday:
        raise ValueError(f"calibration is for intraday intervals, got {interval.code}")
    hi = datetime.combine(end, time.max, UTC)
    lo = datetime.combine(start, time.min, UTC) if start is not None else None
    book_start = lo - max_quote_age if lo is not None and max_quote_age is not None else lo
    quotes = QuoteBook.from_recording(recording, start=book_start, end=hi)
    bars = MinuteBars(read_bars, interval=interval, end=hi)
    rows = load_intraday_tca(
        state, quotes, bars, portfolio_id, since=lo, until=hi, max_quote_age=max_quote_age
    )
    spreads, samples = calibration_evidence(
        rows, quotes, start=lo, end=hi, asset_classes=asset_classes or {}
    )
    limits: dict[str, int] = {}
    if min_quotes is not None:
        limits["min_quotes"] = min_quotes
    if min_fills is not None:
        limits["min_fills"] = min_fills
    return fit_minute_costs(
        spreads, samples, current, end=end, start=start, interval=interval.code, **limits
    )

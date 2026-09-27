"""Live marks and intraday P&L (roadmap 21.3.3, design ``docs/design/intraday.md``).

Three parts, each usable alone:

1. **Marks.** :class:`MarkBook` keeps the latest price per ticker from the
   stream. It is a :class:`~stonks.streaming.runner.StreamRunner` subscriber
   (call it with any event) and an
   :class:`~stonks.engine.driver.EventDriver` handler (``on_bar_close``).
   A trade sets the mark to its price, a quote to its last trade or mid
   (a delayed quote never counts), a bar to its close at the bar's end. An
   event older than the mark never moves it back.
2. **P&L.** :class:`PositionLedger` books one day of one book: the
   positions held at the start of the day, priced at the prior close, and
   every fill of the day at average cost. Realised P&L comes from fills that
   reduce a position, unrealised from the marks, and fees are kept apart.
   ``realised + unrealised - fees`` is the change in the book's value, cash
   included. A book is a whole portfolio or one strategy's sleeve of it
   (the start positions ``position_attribution`` gives the strategy, and
   the fills of the strategy's orders). A fill with no strategy (a manual
   order) counts only in the whole portfolio.
3. **Snapshots.** :class:`IntradayPnlTracker` updates every book on each
   bar close and keeps the day's high-water P&L, then stores one
   ``intraday_snapshots`` row per book every ``snapshot_every`` (five
   minutes by default) and on :meth:`IntradayPnlTracker.finish`. A row
   carries the P&L split, the day's return, the drawdown from the day's
   high, gross and net exposure, and how fresh the marks were.

The trading day is the UTC date of the bar close, which is the session
date for US and European markets. The start of the day is the portfolio's
last snapshot before that date (the previous tick). A name held without a
prior close is priced at its first mark of the day. A fill is applied once
its time has come on the clock, so a replay never sees a later fill early
(P12). The high-water mark survives a restart: it is read back from the
day's stored rows.
"""

from __future__ import annotations

import json
import math
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, time, timedelta
from typing import TYPE_CHECKING, Any, Literal

from pydantic import BaseModel, Field

from stonks.core.clock import SYSTEM_CLOCK, Clock
from stonks.core.stream import QuoteTick, StreamBar, StreamEvent, TradeTick
from stonks.logging import get_logger
from stonks.production.ledger import ledger_filter
from stonks.store.state import SqliteState

if TYPE_CHECKING:
    from stonks.engine.driver import BarClose

__all__ = [
    "PORTFOLIO_BOOK",
    "BookPnl",
    "DayFill",
    "DayStart",
    "IntradayPnlSettings",
    "IntradayPnlTracker",
    "IntradaySnapshot",
    "Mark",
    "MarkBook",
    "MarkedBook",
    "PositionLedger",
    "ReferencePrices",
    "drawdown_from_high",
    "intraday_snapshots_enabled",
    "lake_reference_prices",
    "latest_intraday_day",
    "list_intraday_snapshots",
    "load_day_start",
    "write_intraday_snapshot",
]

_log = get_logger("stonks.production.intraday_pnl")

#: ``strategy_id`` of a whole portfolio's row (a sleeve row names its strategy).
PORTFOLIO_BOOK = ""

MarkSource = Literal["trade", "quote", "bar"]
Side = Literal["buy", "sell"]

#: ``reference_prices(tickers, day)``: the prior close of each ticker before ``day``.
ReferencePrices = Callable[[Sequence[str], date], Mapping[str, float]]


class IntradayPnlSettings(BaseModel):
    """``[production.intraday_pnl]``: marks and intraday snapshots."""

    enabled: bool = False
    #: Store one row per book this often (bar closes in between update the
    #: high-water mark only).
    snapshot_minutes: int = Field(default=5, ge=1, le=390)
    #: A held name whose mark is older than this counts as stale.
    stale_mark_seconds: int = Field(default=120, ge=1)


# ---- marks -------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Mark:
    ticker: str
    price: float
    #: When the price was true: the tick's time, or the bar's end.
    at: datetime
    source: MarkSource


class MarkBook:
    """The latest mark per ticker. Call it with stream events (a runner
    subscriber) or register it on the event driver."""

    name = "marks"

    def __init__(self) -> None:
        self._marks: dict[str, Mark] = {}

    def update(self, ticker: str, price: float, at: datetime, source: MarkSource) -> bool:
        """Set ``ticker``'s mark unless the price is unsound or older than
        the current mark. Returns whether it moved."""
        if not math.isfinite(price) or price <= 0:
            return False
        current = self._marks.get(ticker)
        if current is not None and at < current.at:
            return False
        self._marks[ticker] = Mark(ticker, float(price), at, source)
        return True

    def __call__(self, event: StreamEvent) -> None:
        self.on_event(event)

    def on_event(self, event: StreamEvent) -> None:
        if isinstance(event, TradeTick):
            self.update(event.ticker, event.price, event.timestamp, "trade")
        elif isinstance(event, QuoteTick):
            price = event.reference
            if price is not None and not event.delayed:
                self.update(event.ticker, price, event.timestamp, "quote")
        elif isinstance(event, StreamBar):
            self.update(event.ticker, event.close, _bar_end(event), "bar")

    def on_bar_close(self, event: BarClose) -> None:
        for bar in event.bars:
            self.update(bar.ticker, bar.close, _bar_end(bar), "bar")

    def get(self, ticker: str) -> Mark | None:
        return self._marks.get(ticker)

    def price(self, ticker: str) -> float | None:
        mark = self._marks.get(ticker)
        return None if mark is None else mark.price

    def marks(self) -> dict[str, float]:
        return {t: m.price for t, m in self._marks.items()}

    def age(self, ticker: str, now: datetime) -> timedelta | None:
        mark = self._marks.get(ticker)
        return None if mark is None else now - mark.at


def _bar_end(bar: StreamBar) -> datetime:
    return bar.timestamp + bar.interval.to_timedelta()


# ---- the ledger of one book --------------------------------------------------------


@dataclass(frozen=True, slots=True)
class DayFill:
    fill_id: int
    ticker: str
    side: Side
    #: Shares, always positive: ``side`` gives the sign.
    quantity: float
    price: float
    fee: float
    filled_at: datetime
    #: The strategy of the fill's order, ``None`` for a manual order.
    strategy_id: str | None


@dataclass(frozen=True, slots=True)
class MarkedBook:
    unrealised: float
    #: Signed currency exposure per held ticker.
    exposures: dict[str, float]
    #: Held tickers with no mark, valued at their cost.
    unmarked: tuple[str, ...]


@dataclass
class _Lot:
    quantity: float
    #: Average cost per share, ``None`` until a price is known.
    cost: float | None


_EPS = 1e-9


class PositionLedger:
    """One day of one book at average cost."""

    def __init__(self, start: Mapping[str, float], reference: Mapping[str, float]) -> None:
        self._lots: dict[str, _Lot] = {}
        self._start: dict[str, float] = {}
        for ticker, qty in start.items():
            if abs(qty) > _EPS:
                ref = reference.get(ticker)
                cost = float(ref) if ref is not None and ref > 0 else None
                self._lots[ticker] = _Lot(float(qty), cost)
                self._start[ticker] = float(qty)
        self._start_cost: dict[str, float | None] = {t: lot.cost for t, lot in self._lots.items()}
        self.realised = 0.0
        self.fees = 0.0
        self.fills = 0

    def apply(self, fill: DayFill) -> float:
        """Book ``fill``. Returns the P&L it realised."""
        delta = fill.quantity if fill.side == "buy" else -fill.quantity
        self.fees += fill.fee
        self.fills += 1
        lot = self._lots.setdefault(fill.ticker, _Lot(0.0, None))
        realised = 0.0
        q, cost = lot.quantity, lot.cost
        if abs(q) <= _EPS or (q > 0) == (delta > 0):
            base = fill.price if cost is None else cost
            new_q = q + delta
            lot.cost = (q * base + delta * fill.price) / new_q if abs(new_q) > _EPS else None
            lot.quantity = new_q
        else:
            base = fill.price if cost is None else cost
            closed = min(abs(delta), abs(q))
            sign = 1.0 if q > 0 else -1.0
            realised = sign * closed * (fill.price - base)
            new_q = q + delta
            if abs(new_q) <= _EPS:
                lot.quantity, lot.cost = 0.0, None
            elif (new_q > 0) != (q > 0):  # through zero: the rest opens at the fill
                lot.quantity, lot.cost = new_q, fill.price
            else:
                lot.quantity = new_q
        self.realised += realised
        if abs(lot.quantity) <= _EPS:
            del self._lots[fill.ticker]
        return realised

    def mark(self, marks: Mapping[str, float]) -> MarkedBook:
        """Unrealised P&L and exposure at ``marks``. A held name first seen
        without a price takes its first mark as its cost."""
        unrealised = 0.0
        exposures: dict[str, float] = {}
        unmarked: list[str] = []
        for ticker, lot in sorted(self._lots.items()):
            price = marks.get(ticker)
            if lot.cost is None and price is not None:
                lot.cost = price
                if ticker in self._start_cost and self._start_cost[ticker] is None:
                    self._start_cost[ticker] = price
            if price is None:
                unmarked.append(ticker)
                exposures[ticker] = lot.quantity * (lot.cost or 0.0)
                continue
            unrealised += lot.quantity * (price - (lot.cost if lot.cost is not None else price))
            exposures[ticker] = lot.quantity * price
        return MarkedBook(unrealised, exposures, tuple(unmarked))

    def start_value(self) -> float:
        """The start positions at their start price (net, cash left out)."""
        return sum(q * (self._start_cost.get(t) or 0.0) for t, q in self._start.items())

    def start_gross(self) -> float:
        return sum(abs(q) * (self._start_cost.get(t) or 0.0) for t, q in self._start.items())

    def positions(self) -> dict[str, float]:
        return {t: lot.quantity for t, lot in self._lots.items()}


def drawdown_from_high(*, pnl: float, high: float, base: float) -> float:
    """The drop from the day's high-water P&L as a fraction of the book's
    value at that high (``base`` plus ``high``). Zero or negative."""
    peak_value = base + high
    if peak_value <= 0 or pnl >= high:
        return 0.0
    return (pnl - high) / peak_value


# ---- reading the day from the state ------------------------------------------------


@dataclass(frozen=True)
class DayStart:
    """What a portfolio held when the day began (its last snapshot before it)."""

    portfolio_id: str
    day: date
    #: The snapshot's trading date, ``None`` when there was none.
    as_of: date | None
    cash: float
    positions: dict[str, float]
    #: Shares per strategy per ticker (quantity times weight share).
    sleeves: dict[str, dict[str, float]]


def load_day_start(state: SqliteState, portfolio_id: str, day: date) -> DayStart:
    where, params = ledger_filter(state, "portfolio_snapshots", portfolio_id)
    rows = state.sql(
        f"SELECT as_of, cash, positions_json FROM portfolio_snapshots WHERE {where}"
        " AND as_of IS NOT NULL AND as_of < ? ORDER BY as_of DESC, id DESC LIMIT 1",
        [*params, day.isoformat()],
    )
    if not rows:
        return DayStart(portfolio_id, day, None, 0.0, {}, {})
    row = rows[0]
    as_of = date.fromisoformat(row["as_of"])
    positions = {
        t: float(q) for t, q in json.loads(row["positions_json"] or "{}").items() if float(q)
    }
    return DayStart(
        portfolio_id,
        day,
        as_of,
        float(row["cash"]),
        positions,
        _sleeves(state, portfolio_id, as_of),
    )


def _has_table(state: SqliteState, name: str) -> bool:
    rows = state.sql("SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?", [name])
    return bool(rows)


def _sleeves(state: SqliteState, portfolio_id: str, as_of: date) -> dict[str, dict[str, float]]:
    if not _has_table(state, "position_attribution"):
        return {}
    rows = state.sql(
        "SELECT strategy_id, ticker, quantity, weight_share FROM position_attribution"
        " WHERE portfolio_id = ? AND as_of = ?",
        [portfolio_id, as_of.isoformat()],
    )
    out: dict[str, dict[str, float]] = {}
    for r in rows:
        held = float(r["quantity"]) * float(r["weight_share"])
        if held:
            book = out.setdefault(r["strategy_id"], {})
            book[r["ticker"]] = book.get(r["ticker"], 0.0) + held
    return out


def _utc(value: str) -> datetime:
    parsed = datetime.fromisoformat(value)
    return parsed.replace(tzinfo=UTC) if parsed.tzinfo is None else parsed.astimezone(UTC)


def _day_bounds(day: date) -> tuple[datetime, datetime]:
    start = datetime.combine(day, time(0), tzinfo=UTC)
    return start, start + timedelta(days=1)


def _new_fills(state: SqliteState, portfolio_id: str, day: date, after_id: int) -> list[DayFill]:
    """Fills of ``portfolio_id`` on ``day`` (UTC) with an id above ``after_id``."""
    where, params = ledger_filter(state, "fills", portfolio_id, alias="f")
    lo, hi = _day_bounds(day)
    rows = state.sql(
        "SELECT f.id, f.ticker, f.quantity, f.price, f.fee, f.filled_at, o.side, o.strategy_id"
        " FROM fills f JOIN orders o ON o.client_id = f.order_client_id"
        f" WHERE {where} AND f.id > ? AND f.filled_at >= ? ORDER BY f.id",
        [*params, after_id, (lo - timedelta(days=1)).isoformat()],
    )
    out: list[DayFill] = []
    for r in rows:
        at = _utc(r["filled_at"])
        if not lo <= at < hi:
            continue
        out.append(
            DayFill(
                fill_id=int(r["id"]),
                ticker=r["ticker"],
                side=r["side"],
                quantity=abs(float(r["quantity"])),
                price=float(r["price"]),
                fee=float(r["fee"] or 0.0),
                filled_at=at,
                strategy_id=r["strategy_id"],
            )
        )
    return out


def lake_reference_prices(lake: Any) -> ReferencePrices:
    """Prior closes from the lake's daily ``prices`` (the raw close on the
    last trading day before ``day``)."""

    def read(tickers: Sequence[str], day: date) -> dict[str, float]:
        if not tickers:
            return {}
        df = lake.sql(
            "SELECT ticker, close FROM ("
            " SELECT ticker, close, row_number() OVER (PARTITION BY ticker ORDER BY date DESC)"
            " AS rn FROM prices WHERE ticker = ANY(?) AND date < ?) WHERE rn = 1",
            [sorted(set(tickers)), day],
        )
        return {str(r.ticker): float(r.close) for r in df.itertuples(index=False) if r.close}

    return read


# ---- one book's P&L and its snapshot -----------------------------------------------


@dataclass(frozen=True)
class BookPnl:
    portfolio_id: str
    #: ``PORTFOLIO_BOOK`` for the whole portfolio, else the strategy id.
    strategy_id: str
    day: date
    at: datetime
    #: Portfolio: cash plus positions at the start. Sleeve: gross exposure.
    start_value: float
    #: Portfolio: start value plus P&L. Sleeve: gross exposure now.
    value: float
    realised: float
    unrealised: float
    fees: float
    pnl: float
    day_return: float | None
    gross_exposure: float
    net_exposure: float
    exposures: dict[str, float]
    fills: int
    unmarked: int
    stale_marks: int
    max_mark_age_seconds: float | None


@dataclass(frozen=True)
class IntradaySnapshot:
    portfolio_id: str
    strategy_id: str
    day: date
    at: datetime
    start_value: float
    value: float
    realised: float
    unrealised: float
    fees: float
    pnl: float
    day_return: float | None
    #: The day's best P&L so far (zero or more).
    high_water_pnl: float
    #: The drop from that high over the book's value there (zero or less).
    drawdown: float
    gross_exposure: float
    net_exposure: float
    exposures: Mapping[str, float]
    fills: int
    unmarked: int
    stale_marks: int
    max_mark_age_seconds: float | None
    created_at: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {
            "portfolio_id": self.portfolio_id,
            "strategy_id": self.strategy_id or None,
            "day": self.day,
            "at": self.at,
            "start_value": self.start_value,
            "value": self.value,
            "realised": self.realised,
            "unrealised": self.unrealised,
            "fees": self.fees,
            "pnl": self.pnl,
            "day_return": self.day_return,
            "high_water_pnl": self.high_water_pnl,
            "drawdown": self.drawdown,
            "gross_exposure": self.gross_exposure,
            "net_exposure": self.net_exposure,
            "exposures": dict(self.exposures),
            "fills": self.fills,
            "unmarked": self.unmarked,
            "stale_marks": self.stale_marks,
            "max_mark_age_seconds": self.max_mark_age_seconds,
        }


def intraday_snapshots_enabled(state: SqliteState) -> bool:
    return _has_table(state, "intraday_snapshots")


_COLUMNS = (
    "portfolio_id",
    "strategy_id",
    "day",
    "at",
    "start_value",
    "value",
    "realised",
    "unrealised",
    "fees",
    "pnl",
    "day_return",
    "high_water_pnl",
    "drawdown",
    "gross_exposure",
    "net_exposure",
    "exposures_json",
    "fills",
    "unmarked",
    "stale_marks",
    "max_mark_age_seconds",
    "created_at",
)
_KEY = ("portfolio_id", "strategy_id", "at")


def _iso(when: datetime) -> str:
    return when.astimezone(UTC).isoformat(timespec="seconds")


def write_intraday_snapshot(state: SqliteState, snap: IntradaySnapshot) -> None:
    """Insert ``snap``, replacing the book's row at the same moment."""
    values = [
        snap.portfolio_id,
        snap.strategy_id,
        snap.day.isoformat(),
        _iso(snap.at),
        snap.start_value,
        snap.value,
        snap.realised,
        snap.unrealised,
        snap.fees,
        snap.pnl,
        snap.day_return,
        snap.high_water_pnl,
        snap.drawdown,
        snap.gross_exposure,
        snap.net_exposure,
        json.dumps(dict(sorted(snap.exposures.items()))),
        snap.fills,
        snap.unmarked,
        snap.stale_marks,
        snap.max_mark_age_seconds,
        snap.created_at or datetime.now(UTC).isoformat(timespec="seconds"),
    ]
    marks = ", ".join("?" for _ in _COLUMNS)
    updates = ", ".join(f"{c} = excluded.{c}" for c in _COLUMNS if c not in _KEY)
    state.execute(
        f"INSERT INTO intraday_snapshots ({', '.join(_COLUMNS)}) VALUES ({marks})"
        f" ON CONFLICT (portfolio_id, strategy_id, at) DO UPDATE SET {updates}",
        values,
    )


def _row_snapshot(row: Any) -> IntradaySnapshot:
    return IntradaySnapshot(
        portfolio_id=row["portfolio_id"],
        strategy_id=row["strategy_id"],
        day=date.fromisoformat(row["day"]),
        at=_utc(row["at"]),
        start_value=float(row["start_value"]),
        value=float(row["value"]),
        realised=float(row["realised"]),
        unrealised=float(row["unrealised"]),
        fees=float(row["fees"]),
        pnl=float(row["pnl"]),
        day_return=row["day_return"],
        high_water_pnl=float(row["high_water_pnl"]),
        drawdown=float(row["drawdown"]),
        gross_exposure=float(row["gross_exposure"]),
        net_exposure=float(row["net_exposure"]),
        exposures=json.loads(row["exposures_json"] or "{}"),
        fills=int(row["fills"]),
        unmarked=int(row["unmarked"]),
        stale_marks=int(row["stale_marks"]),
        max_mark_age_seconds=row["max_mark_age_seconds"],
        created_at=row["created_at"],
    )


def latest_intraday_day(state: SqliteState, portfolio_id: str) -> date | None:
    rows = state.sql(
        "SELECT MAX(day) FROM intraday_snapshots WHERE portfolio_id = ?", [portfolio_id]
    )
    value = rows[0][0] if rows else None
    return date.fromisoformat(value) if value else None


def list_intraday_snapshots(
    state: SqliteState,
    portfolio_id: str,
    day: date,
    *,
    strategy_id: str | None = PORTFOLIO_BOOK,
    limit: int = 100,
    offset: int = 0,
) -> tuple[list[IntradaySnapshot], int]:
    """One portfolio's rows on ``day``, newest first: the whole book
    (``PORTFOLIO_BOOK``, the default), one sleeve, or every book (``None``).
    Returns the page and the total count."""
    where = ["portfolio_id = ?", "day = ?"]
    params: list[Any] = [portfolio_id, day.isoformat()]
    if strategy_id is not None:
        where.append("strategy_id = ?")
        params.append(strategy_id)
    clause = " AND ".join(where)
    total = state.sql(f"SELECT COUNT(*) FROM intraday_snapshots WHERE {clause}", params)[0][0]
    rows = state.sql(
        f"SELECT * FROM intraday_snapshots WHERE {clause}"
        " ORDER BY at DESC, strategy_id LIMIT ? OFFSET ?",
        [*params, limit, offset],
    )
    return [_row_snapshot(r) for r in rows], int(total)


# ---- the tracker -------------------------------------------------------------------


@dataclass
class _Book:
    strategy_id: str
    ledger: PositionLedger
    cash: float = 0.0
    high_water_pnl: float = 0.0


@dataclass
class _Portfolio:
    start: DayStart
    books: dict[str, _Book]
    cursor: int = 0
    #: Fetched fills whose time has not come yet on the clock.
    waiting: list[DayFill] = field(default_factory=list)


class IntradayPnlTracker:
    """Intraday P&L of each portfolio and strategy sleeve, updated on every
    bar close and stored every ``snapshot_every``. Register it on the event
    driver (it also feeds its :class:`MarkBook`), or call :meth:`update`."""

    name = "intraday_pnl"

    def __init__(
        self,
        state: SqliteState,
        portfolio_ids: Iterable[str],
        *,
        marks: MarkBook | None = None,
        reference_prices: ReferencePrices | None = None,
        settings: IntradayPnlSettings | None = None,
        clock: Clock = SYSTEM_CLOCK,
    ) -> None:
        cfg = settings or IntradayPnlSettings()
        self.state = state
        self.portfolio_ids = list(dict.fromkeys(portfolio_ids))
        self.marks = marks if marks is not None else MarkBook()
        self.reference_prices = reference_prices
        self.snapshot_every = timedelta(minutes=cfg.snapshot_minutes)
        self.stale_after = timedelta(seconds=cfg.stale_mark_seconds)
        self.clock = clock
        self.snapshots_written = 0
        self._day: date | None = None
        self._portfolios: dict[str, _Portfolio] = {}
        self._last_bucket: int | None = None
        self._last_at: datetime | None = None
        self._last_stored_at: datetime | None = None

    # ---- driver hook ---------------------------------------------------------------

    def on_bar_close(self, event: BarClose) -> list[IntradaySnapshot]:
        self.marks.on_bar_close(event)
        return self.update(event.at)

    def update(self, at: datetime) -> list[IntradaySnapshot]:
        """Recompute every book at ``at``. Returns the rows stored, if the
        snapshot interval came round."""
        books = self.books(at)
        bucket = self._bucket(at)
        if self._last_bucket is not None and bucket == self._last_bucket:
            return []
        self._last_bucket = bucket
        return self._store(books)

    def finish(self, at: datetime | None = None) -> list[IntradaySnapshot]:
        """Store a last row per book (the end of a run or of the session)."""
        when = at or self._last_at
        if when is None or when == self._last_stored_at:
            return []
        return self._store(self.books(when))

    # ---- the books -----------------------------------------------------------------

    def books(self, at: datetime) -> list[BookPnl]:
        """Every book at ``at``, the high-water marks moved along."""
        at = at.astimezone(UTC)
        day = at.date()
        if day != self._day:
            self._start_day(day)
        self._last_at = at
        marks = self.marks.marks()
        out: list[BookPnl] = []
        for pid in self.portfolio_ids:
            pf = self._portfolios[pid]
            self._pull_fills(pid, pf, at)
            for sid in sorted(pf.books):
                out.append(self._book_pnl(pid, pf.books[sid], day, at, marks))
        return out

    def _start_day(self, day: date) -> None:
        self._day = day
        self._last_bucket = None
        self._portfolios = {}
        for pid in self.portfolio_ids:
            start = load_day_start(self.state, pid, day)
            tickers = set(start.positions) | {t for s in start.sleeves.values() for t in s}
            ref = dict(self.reference_prices(sorted(tickers), day)) if self.reference_prices else {}
            books = {PORTFOLIO_BOOK: _Book(PORTFOLIO_BOOK, PositionLedger(start.positions, ref))}
            books[PORTFOLIO_BOOK].cash = start.cash
            for sid, held in start.sleeves.items():
                books[sid] = _Book(sid, PositionLedger(held, ref))
            highs = self._stored_highs(pid, day)
            for sid, book in books.items():
                book.high_water_pnl = highs.get(sid, 0.0)
            self._portfolios[pid] = _Portfolio(start, books)
        _log.info("intraday_pnl.day_started", day=day.isoformat(), portfolios=len(self._portfolios))

    def _stored_highs(self, portfolio_id: str, day: date) -> dict[str, float]:
        if not intraday_snapshots_enabled(self.state):
            return {}
        rows = self.state.sql(
            "SELECT strategy_id, MAX(high_water_pnl) AS hw FROM intraday_snapshots"
            " WHERE portfolio_id = ? AND day = ? GROUP BY strategy_id",
            [portfolio_id, day.isoformat()],
        )
        return {r["strategy_id"]: max(float(r["hw"] or 0.0), 0.0) for r in rows}

    def _pull_fills(self, portfolio_id: str, pf: _Portfolio, at: datetime) -> None:
        fresh = _new_fills(self.state, portfolio_id, pf.start.day, pf.cursor)
        if fresh:
            pf.cursor = max(f.fill_id for f in fresh)
            pf.waiting.extend(fresh)
            pf.waiting.sort(key=lambda f: (f.filled_at, f.fill_id))
        due = [f for f in pf.waiting if f.filled_at <= at]
        if not due:
            return
        pf.waiting = [f for f in pf.waiting if f.filled_at > at]
        for fill in due:
            pf.books[PORTFOLIO_BOOK].ledger.apply(fill)
            if fill.strategy_id:
                book = pf.books.get(fill.strategy_id)
                if book is None:
                    book = _Book(fill.strategy_id, PositionLedger({}, {}))
                    pf.books[fill.strategy_id] = book
                book.ledger.apply(fill)

    def _book_pnl(
        self, pid: str, book: _Book, day: date, at: datetime, marks: Mapping[str, float]
    ) -> BookPnl:
        ledger = book.ledger
        marked = ledger.mark(marks)
        pnl = ledger.realised + marked.unrealised - ledger.fees
        gross = sum(abs(e) for e in marked.exposures.values())
        net = sum(marked.exposures.values())
        if book.strategy_id == PORTFOLIO_BOOK:
            start_value = book.cash + ledger.start_value()
            value = start_value + pnl
        else:
            start_value = ledger.start_gross()
            value = gross
        book.high_water_pnl = max(book.high_water_pnl, pnl)
        ages = [
            age.total_seconds()
            for t in marked.exposures
            if (age := self.marks.age(t, at)) is not None
        ]
        stale = sum(1 for a in ages if a > self.stale_after.total_seconds())
        return BookPnl(
            portfolio_id=pid,
            strategy_id=book.strategy_id,
            day=day,
            at=at,
            start_value=start_value,
            value=value,
            realised=ledger.realised,
            unrealised=marked.unrealised,
            fees=ledger.fees,
            pnl=pnl,
            day_return=pnl / start_value if start_value > 0 else None,
            gross_exposure=gross,
            net_exposure=net,
            exposures={t: e for t, e in marked.exposures.items() if e},
            fills=ledger.fills,
            unmarked=len(marked.unmarked),
            stale_marks=stale,
            max_mark_age_seconds=max(ages) if ages else None,
        )

    def high_water(self, portfolio_id: str, strategy_id: str = PORTFOLIO_BOOK) -> float:
        pf = self._portfolios.get(portfolio_id)
        book = pf.books.get(strategy_id) if pf else None
        return book.high_water_pnl if book else 0.0

    # ---- storing -------------------------------------------------------------------

    def _bucket(self, at: datetime) -> int:
        lo, _ = _day_bounds(at.astimezone(UTC).date())
        return int((at - lo) / self.snapshot_every)

    def _store(self, books: Sequence[BookPnl]) -> list[IntradaySnapshot]:
        if not books or not intraday_snapshots_enabled(self.state):
            return []
        now = self.clock.now().isoformat(timespec="seconds")
        snaps = [self._snapshot(b, now) for b in books]
        with self.state.transaction():
            for snap in snaps:
                write_intraday_snapshot(self.state, snap)
        self.snapshots_written += len(snaps)
        self._last_stored_at = books[0].at
        return snaps

    def _snapshot(self, b: BookPnl, created_at: str) -> IntradaySnapshot:
        high = self.high_water(b.portfolio_id, b.strategy_id)
        return IntradaySnapshot(
            portfolio_id=b.portfolio_id,
            strategy_id=b.strategy_id,
            day=b.day,
            at=b.at,
            start_value=b.start_value,
            value=b.value,
            realised=b.realised,
            unrealised=b.unrealised,
            fees=b.fees,
            pnl=b.pnl,
            day_return=b.day_return,
            high_water_pnl=high,
            drawdown=drawdown_from_high(pnl=b.pnl, high=high, base=b.start_value),
            gross_exposure=b.gross_exposure,
            net_exposure=b.net_exposure,
            exposures=b.exposures,
            fills=b.fills,
            unmarked=b.unmarked,
            stale_marks=b.stale_marks,
            max_mark_age_seconds=b.max_mark_age_seconds,
            created_at=created_at,
        )

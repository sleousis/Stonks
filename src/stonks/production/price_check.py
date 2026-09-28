"""The second-source price check (roadmap 23.6).

Before the tick, the vendor's latest close of every held and signalled
ticker is compared with a second source, and so is its adjusted return over
the last ``adjustment_window_days`` (a split or dividend one source applied
and the other did not shows up there, not in the close).

- A ticker whose close or adjusted return differs by more than the limit is
  a **gap**: the tick holds its opening orders that day (it leaves the
  buyable set, like a stale price) and the operator is alerted. Closes
  still go out (P28: a safety layer never blocks an exit).
- When most compared tickers gap, the gap is **systematic** (the vendor is
  wrong, not one ticker): the global ``operational`` halt opens, and the
  ``price_check`` health check keeps it open until a later check is not
  systematic, then the next health run clears it.
- A ticker neither source prices is ``unknown``: shown, never held.

Second sources sit behind :class:`SecondSource`: :class:`DataSourceCloses`
wraps any :class:`~stonks.ingest.sources.base.DataSource` (Yahoo by
default), :class:`BrokerMarks` reads a broker's quotes (IBKR marks for held
tickers when a gateway is set), closes only and in major units.

Every run is one ``price_checks`` row (SQLite migration 052). The tick
reads the held tickers of its day with :func:`price_holds`, and Health
shows :func:`price_check_health`.
"""

from __future__ import annotations

import json
from abc import ABC, abstractmethod
from collections.abc import Callable, Mapping, Sequence
from dataclasses import asdict, dataclass
from datetime import UTC, date, datetime, timedelta
from typing import Any, Literal

import pandas as pd

from stonks.fx.units import price_scales
from stonks.logging import get_logger
from stonks.production.health import HealthCheck
from stonks.production.price_check_settings import PriceCheckSettings
from stonks.store.state import SqliteState

_log = get_logger("stonks.production.price_check")

TABLE = "price_checks"
ACTOR = "service:price_check"
ItemStatus = Literal["ok", "gap", "unknown"]
CheckStatus = Literal["clean", "gaps", "systematic", "unavailable"]


# ---- second sources ----------------------------------------------------------------------


@dataclass(frozen=True)
class SecondBar:
    day: date
    close: float
    adj_close: float | None = None


class SecondSource(ABC):
    """Where the second opinion comes from."""

    name: str = "second"
    #: Prices in the major currency unit (pounds, not pence).
    major_units: bool = False
    #: A bar of any day matches the vendor's latest close (live marks).
    any_day: bool = False

    @abstractmethod
    def series(
        self, tickers: Sequence[str], since: date, until: date
    ) -> dict[str, list[SecondBar] | Exception]:
        """Daily bars per ticker (oldest first), or the error it raised."""


class DataSourceCloses(SecondSource):
    """Any ``DataSource``: daily closes and adjusted closes in the lake's
    units. A ticker whose fetch fails is ``unknown``."""

    def __init__(self, source: Any) -> None:
        self._source = source
        self.name = str(getattr(source, "source_id", "second"))

    def series(
        self, tickers: Sequence[str], since: date, until: date
    ) -> dict[str, list[SecondBar] | Exception]:
        out: dict[str, list[SecondBar] | Exception] = {}
        for ticker in tickers:
            try:
                rows = self._source.fetch_prices(ticker, since=since, until=until)
            except Exception as exc:
                out[ticker] = exc
                continue
            bars = [
                SecondBar(
                    r.date, float(r.close), None if r.adj_close is None else float(r.adj_close)
                )
                for r in rows
                if r.close is not None and r.close > 0
            ]
            out[ticker] = sorted(bars, key=lambda b: b.day)
        return out


class BrokerMarks(SecondSource):
    """A broker's quotes (their reference price) as today's mark, in the
    major unit. Closes only: a mark has no adjusted history."""

    major_units = True
    any_day = True

    def __init__(self, broker: Any, name: str = "broker") -> None:
        self._broker = broker
        self.name = name

    def series(
        self, tickers: Sequence[str], since: date, until: date
    ) -> dict[str, list[SecondBar] | Exception]:
        try:
            quotes = self._broker.quotes(list(tickers))
        except Exception as exc:
            return dict.fromkeys(tickers, exc)
        out: dict[str, list[SecondBar] | Exception] = {}
        for ticker in tickers:
            quote = quotes.get(ticker)
            ref = getattr(quote, "reference", None) if quote is not None else None
            out[ticker] = [SecondBar(until, float(ref))] if ref is not None and ref > 0 else []
        return out


# ---- the check ---------------------------------------------------------------------------


@dataclass(frozen=True)
class PriceCheckItem:
    ticker: str
    status: ItemStatus
    detail: str
    source: str = ""
    vendor_date: date | None = None
    vendor_close: float | None = None
    second_close: float | None = None
    close_gap: float | None = None
    adjustment_gap: float | None = None

    def as_dict(self) -> dict[str, Any]:
        out = asdict(self)
        out["vendor_date"] = self.vendor_date.isoformat() if self.vendor_date else None
        return out


@dataclass(frozen=True)
class PriceCheckReport:
    as_of: date
    source: str
    status: CheckStatus
    items: tuple[PriceCheckItem, ...]
    detail: str = ""

    @property
    def held(self) -> tuple[str, ...]:
        return tuple(sorted(i.ticker for i in self.items if i.status == "gap"))

    @property
    def compared(self) -> int:
        return sum(1 for i in self.items if i.status != "unknown")


def _vendor_frame(lake: Any, tickers: Sequence[str], since: date, as_of: date) -> pd.DataFrame:
    return lake.sql(
        "SELECT ticker, date, close, adj_close FROM prices"
        " WHERE ticker = ANY(?) AND date >= ? AND date <= ? ORDER BY ticker, date",
        [list(tickers), since, as_of],
    )


def _day(value: Any) -> date:
    """A lake date (a date, a datetime or a pandas Timestamp) as a date."""
    return date.fromisoformat(pd.Timestamp(value).strftime("%Y-%m-%d"))


def _gap(a: float, b: float) -> float:
    return abs(a - b) / abs(b) if b else float("inf")


def _compare_one(
    ticker: str,
    vendor: pd.DataFrame | None,
    second: list[SecondBar] | Exception | None,
    source: SecondSource,
    scale: float,
    settings: PriceCheckSettings,
) -> PriceCheckItem:
    if vendor is None or vendor.empty:
        return PriceCheckItem(ticker, "unknown", "the vendor has no recent close", source.name)
    if isinstance(second, Exception):
        return PriceCheckItem(ticker, "unknown", f"{source.name} failed: {second}", source.name)
    if not second:
        return PriceCheckItem(ticker, "unknown", f"{source.name} has no price", source.name)
    last = vendor.iloc[-1]
    vendor_day = _day(last["date"])
    vendor_close = float(last["close"]) * (scale if source.major_units else 1.0)
    by_day = {b.day: b for b in second}
    match = second[-1] if source.any_day else by_day.get(vendor_day)
    if match is None:
        return PriceCheckItem(
            ticker, "unknown", f"{source.name} has no bar on {vendor_day}", source.name,
            vendor_day, vendor_close,
        )  # fmt: skip
    close_gap = _gap(vendor_close, match.close)
    adjustment_gap = _adjustment_gap(vendor, by_day, match)
    reasons = []
    if close_gap > settings.max_close_gap:
        reasons.append(f"close {close_gap:.1%} off (limit {settings.max_close_gap:.1%})")
    if adjustment_gap is not None and adjustment_gap > settings.max_adjustment_gap:
        reasons.append(
            f"adjusted return {adjustment_gap:.1%} off (limit {settings.max_adjustment_gap:.1%}):"
            " a split or dividend one source missed"
        )
    return PriceCheckItem(
        ticker,
        "gap" if reasons else "ok",
        "; ".join(reasons) or "matches",
        source.name,
        vendor_day,
        vendor_close,
        match.close,
        close_gap,
        adjustment_gap,
    )


def _adjustment_gap(
    vendor: pd.DataFrame, by_day: Mapping[date, SecondBar], last: SecondBar
) -> float | None:
    """How far the two adjusted returns from the oldest common day to the
    latest one differ, or ``None`` when they cannot be compared."""
    if last.adj_close is None:
        return None
    end = vendor.iloc[-1]
    for row in vendor.to_dict("records"):
        day = _day(row["date"])
        first = by_day.get(day)
        if first is None or first.adj_close is None or day >= last.day:
            continue
        start_adj = row["adj_close"]
        if not start_adj or not end["adj_close"] or not first.adj_close or not last.adj_close:
            return None
        vendor_return = float(end["adj_close"]) / float(start_adj)
        second_return = last.adj_close / first.adj_close
        return abs(vendor_return / second_return - 1.0)
    return None


def check_prices(
    lake: Any,
    second: SecondSource,
    tickers: Sequence[str],
    as_of: date,
    settings: PriceCheckSettings,
) -> PriceCheckReport:
    """Compare ``tickers`` against one second source."""
    return _report(as_of, second.name, _items(lake, second, tickers, as_of, settings), settings)


def _items(
    lake: Any,
    second: SecondSource,
    tickers: Sequence[str],
    as_of: date,
    settings: PriceCheckSettings,
) -> list[PriceCheckItem]:
    wanted = list(dict.fromkeys(tickers))
    if not wanted:
        return []
    since = as_of - timedelta(days=settings.adjustment_window_days)
    frame = _vendor_frame(lake, wanted, since, as_of)
    vendor = {str(t): g for t, g in frame.groupby("ticker", sort=False)} if len(frame) else {}
    answers = second.series(wanted, since - timedelta(days=7), as_of)
    scales = price_scales(lake, wanted) if second.major_units else {}
    return [
        _compare_one(t, vendor.get(t), answers.get(t), second, scales.get(t, 1.0), settings)
        for t in wanted
    ]


def _report(
    as_of: date, source: str, items: Sequence[PriceCheckItem], settings: PriceCheckSettings
) -> PriceCheckReport:
    compared = [i for i in items if i.status != "unknown"]
    gaps = [i for i in compared if i.status == "gap"]
    status: CheckStatus
    if not compared:
        status = "unavailable"
    elif (
        len(compared) >= settings.min_systematic_tickers
        and len(gaps) / len(compared) >= settings.systematic_share
    ):
        status = "systematic"
    elif gaps:
        status = "gaps"
    else:
        status = "clean"
    detail = f"{len(gaps)} of {len(compared)} compared tickers differ from {source}"
    return PriceCheckReport(as_of, source, status, tuple(items), detail)


# ---- what to check -----------------------------------------------------------------------


def _table(state: SqliteState, name: str) -> bool:
    rows = state.sql("SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?", [name])
    return bool(rows)


def held_by_portfolio(state: SqliteState) -> dict[str, list[str]]:
    """Each portfolio's held tickers in its latest snapshot (options left out)."""
    rows = state.sql(
        "SELECT s.portfolio_id, s.positions_json FROM portfolio_snapshots s"
        " JOIN (SELECT portfolio_id, MAX(id) AS id FROM portfolio_snapshots"
        "       GROUP BY portfolio_id) last ON last.id = s.id"
    )
    out: dict[str, list[str]] = {}
    for r in rows:
        try:
            positions = json.loads(r["positions_json"] or "{}")
        except ValueError:
            continue
        tickers = sorted(
            t for t, q in positions.items() if ":" not in t and abs(float(q or 0.0)) > 1e-12
        )
        if tickers:
            out[str(r["portfolio_id"])] = tickers
    return out


def tickers_to_check(state: SqliteState, as_of: date) -> tuple[list[str], list[str]]:
    """``(held, signalled)``: tickers of the latest snapshots, then the
    tickers of the latest stored signals on or before ``as_of`` not held."""
    held = sorted({t for tickers in held_by_portfolio(state).values() for t in tickers})
    signalled: list[str] = []
    if _table(state, "signals"):
        rows = state.sql(
            "SELECT DISTINCT ticker FROM signals WHERE as_of ="
            " (SELECT MAX(as_of) FROM signals WHERE as_of <= ?) ORDER BY ticker",
            [as_of.isoformat()],
        )
        mine = set(held)
        signalled = [
            r["ticker"] for r in rows if r["ticker"] not in mine and ":" not in r["ticker"]
        ]
    return held, signalled


# ---- storing and reading -----------------------------------------------------------------


def checks_enabled(state: SqliteState) -> bool:
    return _table(state, TABLE)


def record_check(
    state: SqliteState,
    report: PriceCheckReport,
    *,
    checked_at: datetime,
    halt_id: int | None = None,
) -> int:
    cur = state.execute(
        f"INSERT INTO {TABLE} (as_of, checked_at, source, status, tickers_checked,"
        " tickers_compared, held_json, items_json, detail, halt_id)"
        " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        [
            report.as_of.isoformat(),
            checked_at.isoformat(timespec="seconds"),
            report.source,
            report.status,
            len(report.items),
            report.compared,
            json.dumps(list(report.held)),
            json.dumps([i.as_dict() for i in report.items], default=str),
            report.detail,
            halt_id,
        ],
    )
    return int(cur.lastrowid or 0)


def latest_check(state: SqliteState, as_of: date | None = None) -> dict[str, Any] | None:
    """The newest ``price_checks`` row (of ``as_of`` when given), decoded."""
    if not checks_enabled(state):
        return None
    if as_of is None:
        rows = state.sql(f"SELECT * FROM {TABLE} ORDER BY id DESC LIMIT 1")
    else:
        rows = state.sql(
            f"SELECT * FROM {TABLE} WHERE as_of = ? ORDER BY id DESC LIMIT 1", [as_of.isoformat()]
        )
    if not rows:
        return None
    row = dict(rows[0])
    row["held"] = json.loads(row.pop("held_json") or "[]")
    row["items"] = json.loads(row.pop("items_json") or "[]")
    return row


def price_holds(state: SqliteState, as_of: date) -> frozenset[str]:
    """Tickers whose opening orders the tick holds on ``as_of``."""
    row = latest_check(state, as_of)
    return frozenset(row["held"]) if row else frozenset()


def price_check_health(state: SqliteState) -> list[HealthCheck]:
    """``price_check`` fails while the newest check is systematic (it keeps
    the operational halt open). ``price_gap`` fails while it held tickers."""
    if not checks_enabled(state):
        return []
    row = latest_check(state)
    if row is None:
        return [HealthCheck("price_check", True, "no price check yet")]
    when = f"{row['source']} on {row['as_of']}"
    systematic = row["status"] == "systematic"
    checks = [
        HealthCheck(
            "price_check",
            not systematic,
            f"{row['detail']} ({when})" if systematic else f"{row['status']} ({when})",
        )
    ]
    held = row["held"]
    checks.append(
        HealthCheck(
            "price_gap",
            not held,
            f"orders held for {', '.join(held[:10])} ({when})" if held else "no gap",
        )
    )
    return checks


# ---- running it --------------------------------------------------------------------------


def run_price_checks(
    state: SqliteState,
    lake: Any,
    groups: Sequence[tuple[SecondSource, Sequence[str]]],
    as_of: date,
    settings: PriceCheckSettings,
    *,
    now: datetime | None = None,
    publish: Callable[[Any], Any] | None = None,
) -> PriceCheckReport:
    """Check each group of tickers against its source, store one row, and
    open the operational halt on a systematic gap."""
    from stonks.production.halts import halts_enabled, notify_trip, trip_halt

    items: list[PriceCheckItem] = []
    names: list[str] = []
    for source, tickers in groups:
        if tickers:
            items.extend(_items(lake, source, tickers, as_of, settings))
            names.append(source.name)
    report = _report(as_of, "+".join(names) or "none", items, settings)
    when = now or datetime.now(UTC)
    halt_id: int | None = None
    if report.status == "systematic" and halts_enabled(state):
        halt, created = trip_halt(
            state,
            "operational",
            reason=f"price check: {report.detail}",
            actor=ACTOR,
            scope="global",
            on=as_of,
        )
        halt_id = halt.id
        if created:
            notify_trip(state, halt, publish)
    record_check(state, report, checked_at=when, halt_id=halt_id)
    _log.info(
        "price_check.done",
        as_of=as_of.isoformat(),
        status=report.status,
        compared=report.compared,
        held=list(report.held),
    )
    return report


def run_price_check(
    state: SqliteState,
    lake: Any,
    second: SecondSource,
    tickers: Sequence[str],
    as_of: date,
    settings: PriceCheckSettings,
    *,
    now: datetime | None = None,
    publish: Callable[[Any], Any] | None = None,
) -> PriceCheckReport:
    return run_price_checks(
        state, lake, [(second, tickers)], as_of, settings, now=now, publish=publish
    )


def build_groups(
    state: SqliteState,
    as_of: date,
    settings: PriceCheckSettings,
    source: SecondSource,
    marks: Callable[[str], SecondSource | None] | None = None,
) -> list[tuple[SecondSource, list[str]]]:
    """The tickers to check and the source each goes to: held tickers of a
    portfolio ``marks`` gives a broker source for go to its marks, the rest
    (held elsewhere, then signalled) to ``source``. At most
    ``max_tickers`` in all, held first."""
    budget = settings.max_tickers
    groups: list[tuple[SecondSource, list[str]]] = []
    marked: set[str] = set()
    if marks is not None and settings.use_broker_marks:
        for portfolio_id, tickers in sorted(held_by_portfolio(state).items()):
            broker_source = marks(portfolio_id)
            if broker_source is None:
                continue
            chosen = [t for t in tickers if t not in marked][:budget]
            budget -= len(chosen)
            marked.update(chosen)
            if chosen:
                groups.append((broker_source, chosen))
    held, signalled = tickers_to_check(state, as_of)
    rest = [t for t in [*held, *signalled] if t not in marked][: max(budget, 0)]
    groups.append((source, rest))
    return groups

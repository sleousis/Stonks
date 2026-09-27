"""Corporate actions on the production portfolios (real and shadow).

The tick values and sizes on **raw** quotes, so a stored portfolio must be
brought to the share basis those quotes are in before anything decides:
on each ex-date a split multiplies held quantities (and the quantities of
orders still working from earlier ticks), and a cash dividend credits
``quantity x amount x (1 - withholding_rate)``. The per-position rules are
the backtest's (:func:`stonks.backtest.corporate_actions.apply_to_portfolio`),
so research and production account for an event identically.

Exactly once, derived from snapshots
------------------------------------
A portfolio snapshot taken **as of** day ``S`` is in the share basis of
the closes on or before ``S``, i.e. it already reflects every event with
``ex_date <= S`` (a position bought on an ex-date was bought at the
post-event price). A tick as of ``D`` therefore applies exactly the events
with ``S < ex_date <= D`` to the portfolio loaded from ``S`` and persists
the result in its own snapshot as of ``D``. Rerunning ``D`` loads that
snapshot and finds nothing due; a tick that crashes (or is a dry run)
before writing its snapshot persisted nothing, so the next tick derives
the same events again from ``S``. No event with ``ex_date > D`` is ever
applied (no look-ahead), and a portfolio with no dated snapshot (freshly
seeded, or legacy rows without ``as_of``) has nothing due.

The since-window rule above is what model books (``production.shadow``)
and pre-ledger state files still use. It assumes the event row and the
ex-date bar are both in the lake when the ex-date tick runs.

Real books: the ledger (TO-05)
------------------------------
Real portfolios keep a ``corporate_action_ledger``: each event is handled
once per portfolio, whenever its row reaches the lake, and only once the
lake holds a bar for its ticker on or after the ex-date (before that the
quote is still in the pre-event basis; the event is deferred and reported).
The quantity an event acts on is the one held at the close before the
ex-date (the latest snapshot dated before it, in the ex-date's share
basis), so a late split scales only the shares held before its ex-date:
shares bought later were bought at post-split quotes. A position that was
closed in between is skipped. The ledger rows are written in the
transaction that writes the tick's snapshot, so a crash or a dry run
persists nothing and a rerun finds the events handled.
"""

from __future__ import annotations

import json
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import date, datetime, time
from typing import Any

from stonks.backtest.corporate_actions import (
    CorporateActionRecord,
    apply_to_portfolio,
    dividend_cash,
)
from stonks.core.corporate_actions import CorporateAction, CorporateActions, Dividend, Split
from stonks.core.types import Portfolio
from stonks.execution.reconcile import NON_TERMINAL_STATUSES
from stonks.production.ledger import ledger_filter
from stonks.store.corporate_actions import LakeCorporateActions
from stonks.store.state import SqliteState


def load_corporate_actions(lake: object, tickers: Iterable[str]) -> CorporateActions:
    """Every known event for ``tickers`` (one lake query)."""
    unique = sorted(set(tickers))
    return LakeCorporateActions(lake).load(unique) if unique else CorporateActions()


def events_due(
    actions: CorporateActions, *, since: date | None, as_of: date
) -> list[CorporateAction]:
    """Events with ``since < ex_date <= as_of``, oldest first (splits
    before dividends on one ex-date); none when ``since`` is ``None``."""
    if since is None or since >= as_of:
        return []
    due = [
        event
        for events in actions.by_ticker.values()
        for event in events
        if since < event.ex_date <= as_of
    ]
    return sorted(due, key=lambda e: (e.ex_date, isinstance(e, Dividend), e.ticker))


def apply_corporate_actions(
    portfolio: Portfolio,
    actions: CorporateActions,
    *,
    since: date | None,
    as_of: date,
    withholding_rate: float = 0.0,
) -> list[CorporateActionRecord]:
    """Apply the events due in ``(since, as_of]`` to ``portfolio`` in place;
    returns one record per event that touched a held position."""
    records: list[CorporateActionRecord] = []
    for event in events_due(actions, since=since, as_of=as_of):
        record = apply_to_portfolio(
            portfolio,
            event,
            datetime.combine(event.ex_date, time()),
            withholding_rate=withholding_rate,
        )
        if record is not None:
            records.append(record)
    return records


def working_orders(state: SqliteState) -> dict[str, str]:
    """``client_id -> ticker`` of ledger orders still working (not filled /
    rejected / cancelled). Captured before a tick places anything, these
    are exactly the orders sized in an earlier tick, in pre-event shares."""
    placeholders = ",".join("?" for _ in NON_TERMINAL_STATUSES)
    rows = state.sql(
        f"SELECT client_id, ticker FROM orders WHERE status IN ({placeholders}) ORDER BY client_id",
        list(NON_TERMINAL_STATUSES),
    )
    return {r["client_id"]: r["ticker"] for r in rows}


def adjust_working_orders(
    state: SqliteState,
    client_ids: Sequence[str],
    actions: CorporateActions,
    *,
    since: date | None,
    as_of: date,
    now: str,
) -> int:
    """Re-express the listed working orders in post-split shares (quantity
    x ratio, limit price / ratio) for every split due in ``(since, as_of]``.
    Call inside the transaction that writes the tick's snapshot, so the
    adjustment persists exactly when the snapshot that marks the splits as
    applied does. Returns the number of order rows updated."""
    splits = [e for e in events_due(actions, since=since, as_of=as_of) if isinstance(e, Split)]
    if not client_ids or not splits:
        return 0
    status_ph = ",".join("?" for _ in NON_TERMINAL_STATUSES)
    ids_ph = ",".join("?" for _ in client_ids)
    touched = 0
    for split in splits:
        _rescale_fills(state, client_ids, split, status_ph)
        stop, stop_arg = _stop_rescale(state, split.ratio)
        cursor = state.execute(
            "UPDATE orders SET quantity = quantity * ?,"
            f" limit_price = limit_price / ?{stop}, updated_at = ?"
            f" WHERE ticker = ? AND status IN ({status_ph}) AND client_id IN ({ids_ph})",
            [split.ratio, split.ratio, *stop_arg, now, split.ticker, *NON_TERMINAL_STATUSES,
             *client_ids],  # fmt: skip
        )
        touched += cursor.rowcount
    return touched


def record_as_dict(record: CorporateActionRecord) -> dict[str, object]:
    """JSON-ready form for tick summaries."""
    return {
        "ex_date": record.timestamp.date().isoformat(),
        "ticker": record.ticker,
        "kind": record.kind,
        "value": record.value,
        "quantity_before": record.quantity_before,
        "quantity_after": record.quantity_after,
        "cash_delta": record.cash_delta,
    }


# ---- the per-portfolio ledger (TO-05) --------------------------------------------

LEDGER = "corporate_action_ledger"
LEDGER_START = "corporate_action_ledger_start"


def ledger_enabled(state: SqliteState) -> bool:
    """Whether the ledger exists (migration 018 applied)."""
    rows = state.sql("SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?", [LEDGER])
    return bool(rows)


def _kind(event: CorporateAction) -> str:
    return "split" if isinstance(event, Split) else "dividend"


def _value(event: CorporateAction) -> float:
    return event.ratio if isinstance(event, Split) else event.amount


def event_as_dict(event: CorporateAction) -> dict[str, object]:
    return {
        "ex_date": event.ex_date.isoformat(),
        "ticker": event.ticker,
        "kind": _kind(event),
        "value": _value(event),
    }


@dataclass(frozen=True)
class PlannedAction:
    event: CorporateAction
    #: Shares held at the close before the ex-date, in the ex-date's basis.
    base_quantity: float


@dataclass
class CorporateActionPlan:
    """What a tick does with the events of one portfolio."""

    #: Due now: applied to the portfolio and recorded in the ledger.
    due: list[PlannedAction] = field(default_factory=list)
    #: Due by date, but the ex-date bar is not in the lake yet.
    deferred: list[CorporateAction] = field(default_factory=list)

    @property
    def splits(self) -> list[Split]:
        return [p.event for p in self.due if isinstance(p.event, Split)]


def plan_corporate_actions(
    state: SqliteState,
    lake: Any,
    actions: CorporateActions,
    *,
    portfolio_id: str,
    as_of: date,
) -> CorporateActionPlan:
    """The events of ``actions`` not yet handled for ``portfolio_id`` with
    ``ex_date <= as_of`` (after the ledger's start, if it has one), split
    into due (ex-date bar present) and deferred."""
    plan = CorporateActionPlan()
    start = _ledger_start(state, portfolio_id)
    handled = _handled(state, portfolio_id)
    candidates = [
        event
        for events in actions.by_ticker.values()
        for event in events
        if event.ex_date <= as_of
        and (start is None or event.ex_date > start)
        and (event.ticker, event.ex_date.isoformat(), _kind(event)) not in handled
    ]
    if not candidates:
        return plan
    candidates.sort(key=lambda e: (e.ex_date, isinstance(e, Dividend), e.ticker))
    last_bar = _last_bar_dates(lake, sorted({e.ticker for e in candidates}), as_of)
    history = _snapshot_history(state, portfolio_id)
    for event in candidates:
        seen = last_bar.get(event.ticker)
        if seen is None or seen < event.ex_date:
            plan.deferred.append(event)
            continue
        plan.due.append(PlannedAction(event, _base_quantity(history, event, actions)))
    return plan


def apply_plan(
    portfolio: Portfolio, plan: CorporateActionPlan, *, withholding_rate: float = 0.0
) -> list[CorporateActionRecord]:
    """Apply the due events to ``portfolio`` in place. A split adds
    ``(ratio - 1) x base_quantity`` shares (never crossing zero); a dividend
    credits ``base_quantity x amount x (1 - withholding_rate)``. The base is
    signed: a short grows short on a split and pays the dividend in full
    (roadmap 16.1). Returns one record per event that changed the
    portfolio."""
    records: list[CorporateActionRecord] = []
    for planned in plan.due:
        event, base = planned.event, planned.base_quantity
        if base == 0:
            continue
        held = portfolio.positions.get(event.ticker, 0.0)
        stamp = datetime.combine(event.ex_date, time())
        if isinstance(event, Split):
            after = held + (event.ratio - 1.0) * base
            after = max(after, 0.0) if base > 0 else min(after, 0.0)
            if after != 0:
                portfolio.positions[event.ticker] = after
            else:
                portfolio.positions.pop(event.ticker, None)
            records.append(
                CorporateActionRecord(stamp, event.ticker, "split", event.ratio, held, after, 0.0)
            )
        else:
            cash = dividend_cash(base, event.amount, withholding_rate)
            portfolio.cash += cash
            records.append(
                CorporateActionRecord(
                    stamp, event.ticker, "dividend", event.amount, held, held, cash
                )
            )
    return records


def record_plan(
    state: SqliteState,
    plan: CorporateActionPlan,
    records: Sequence[CorporateActionRecord],
    *,
    portfolio_id: str,
    tick_id: str,
    now: str,
) -> None:
    """Write one ledger row per due event (call inside the snapshot's
    transaction)."""
    by_key = {(r.ticker, r.timestamp.date(), r.kind): r for r in records}
    for planned in plan.due:
        event = planned.event
        rec = by_key.get((event.ticker, event.ex_date, _kind(event)))
        state.execute(
            f"INSERT OR IGNORE INTO {LEDGER} (portfolio_id, ticker, ex_date, kind, value,"
            " quantity_before, quantity_after, cash_delta, tick_id, applied_at)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            [
                portfolio_id,
                event.ticker,
                event.ex_date.isoformat(),
                _kind(event),
                _value(event),
                rec.quantity_before if rec else 0.0,
                rec.quantity_after if rec else 0.0,
                rec.cash_delta if rec else 0.0,
                tick_id,
                now,
            ],
        )


def adjust_orders_for_splits(
    state: SqliteState, client_ids: Sequence[str], splits: Sequence[Split], *, now: str
) -> int:
    """Re-express working orders decided before each split's ex-date in
    post-split shares. An order's decision date is its client id's leading
    date (``make_client_id``); an order without one counts as earlier."""
    if not client_ids or not splits:
        return 0
    status_ph = ",".join("?" for _ in NON_TERMINAL_STATUSES)
    touched = 0
    for split in splits:
        earlier = [c for c in client_ids if _decided_before(c, split.ex_date)]
        if not earlier:
            continue
        ids_ph = ",".join("?" for _ in earlier)
        _rescale_fills(state, earlier, split, status_ph)
        stop, stop_arg = _stop_rescale(state, split.ratio)
        cursor = state.execute(
            "UPDATE orders SET quantity = quantity * ?,"
            f" limit_price = limit_price / ?{stop}, updated_at = ?"
            f" WHERE ticker = ? AND status IN ({status_ph}) AND client_id IN ({ids_ph})",
            [split.ratio, split.ratio, *stop_arg, now, split.ticker, *NON_TERMINAL_STATUSES,
             *earlier],  # fmt: skip
        )
        touched += cursor.rowcount
    return touched


def _stop_rescale(state: SqliteState, ratio: float) -> tuple[str, list[float]]:
    """The ``stop_price`` part of a split's order update (migration 028): a
    working stop (a protective stop, roadmap 19.10) moves with the shares."""
    if not any(
        r["name"] == "stop_price" for r in state.sql("SELECT name FROM pragma_table_info('orders')")
    ):
        return "", []
    return ", stop_price = stop_price / ?", [ratio]


def _rescale_fills(
    state: SqliteState, client_ids: Sequence[str], split: Split, status_ph: str
) -> None:
    """Move the fills already booked for the working orders to post-split
    shares (quantity x ratio, prices / ratio, notional unchanged). The
    broker reports its cumulative fill in post-split shares, so without
    this the next reconcile would book the difference as a new fill."""
    ids_ph = ",".join("?" for _ in client_ids)
    arrival = ", arrival_price = arrival_price / ?" if _has_arrival(state) else ""
    state.execute(
        f"UPDATE fills SET quantity = quantity * ?, price = price / ?{arrival}"
        f" WHERE ticker = ? AND order_client_id IN ({ids_ph}) AND order_client_id IN"
        f" (SELECT client_id FROM orders WHERE status IN ({status_ph}))",
        [
            split.ratio,
            split.ratio,
            *([split.ratio] if arrival else []),
            split.ticker,
            *client_ids,
            *NON_TERMINAL_STATUSES,
        ],
    )


def _has_arrival(state: SqliteState) -> bool:
    cols = {r["name"] for r in state.sql("SELECT name FROM pragma_table_info('fills')")}
    return "arrival_price" in cols


def _decided_before(client_id: str, ex_date: date) -> bool:
    try:
        decided = date.fromisoformat(client_id[:10])
    except ValueError:
        return True
    return decided < ex_date


def _ledger_start(state: SqliteState, portfolio_id: str) -> date | None:
    rows = state.sql(
        f"SELECT start_as_of FROM {LEDGER_START} WHERE portfolio_id = ?", [portfolio_id]
    )
    return date.fromisoformat(rows[0]["start_as_of"]) if rows else None


def _handled(state: SqliteState, portfolio_id: str) -> set[tuple[str, str, str]]:
    rows = state.sql(
        f"SELECT ticker, ex_date, kind FROM {LEDGER} WHERE portfolio_id = ?", [portfolio_id]
    )
    return {(r["ticker"], r["ex_date"], r["kind"]) for r in rows}


def _last_bar_dates(lake: Any, tickers: Sequence[str], as_of: date) -> dict[str, date]:
    """Each ticker's latest daily bar date on or before ``as_of``."""
    if not tickers or not hasattr(lake, "sql"):
        return {}
    df = lake.sql(
        "SELECT ticker, MAX(date) AS latest FROM prices"
        " WHERE ticker = ANY(?) AND date <= ? GROUP BY ticker",
        [list(tickers), as_of],
    )
    out: dict[str, date] = {}
    for row in df.itertuples(index=False):
        latest = row.latest
        out[row.ticker] = latest.date() if isinstance(latest, datetime) else latest
    return out


def _snapshot_history(state: SqliteState, portfolio_id: str) -> list[tuple[date, dict]]:
    """The portfolio's tick snapshots, one per day (a rerun's later one
    wins), oldest first."""
    where, params = ledger_filter(state, "portfolio_snapshots", portfolio_id, tick_only=True)
    rows = state.sql(
        f"SELECT as_of, positions_json FROM portfolio_snapshots WHERE {where}"
        " AND as_of IS NOT NULL ORDER BY as_of, id",
        params,
    )
    by_day: dict[date, dict] = {}
    for r in rows:
        by_day[date.fromisoformat(r["as_of"])] = json.loads(r["positions_json"])
    return sorted(by_day.items())


def _base_quantity(
    history: Sequence[tuple[date, Mapping[str, float]]],
    event: CorporateAction,
    actions: CorporateActions,
) -> float:
    """Shares of ``event.ticker`` held at the close before its ex-date, in
    the ex-date's basis: the latest snapshot dated before the ex-date,
    scaled by the splits in between. Zero when there is none, or when the
    position was closed at any snapshot since (bought back later means
    bought at post-event quotes). Signed: a short's base is negative, and
    a flip from short to long (or back) in between counts as closed."""
    before = [(day, pos) for day, pos in history if day < event.ex_date]
    if not before:
        return 0.0
    day0, positions0 = before[-1]
    quantity = float(positions0.get(event.ticker, 0.0))
    if quantity == 0:
        return 0.0
    for day, positions in history:
        if day > day0 and float(positions.get(event.ticker, 0.0)) * quantity <= 0:
            return 0.0
    for other in actions.by_ticker.get(event.ticker, ()):
        if not isinstance(other, Split) or other is event:
            continue
        if day0 < other.ex_date < event.ex_date or (
            isinstance(event, Dividend) and other.ex_date == event.ex_date
        ):
            quantity *= other.ratio
    return quantity

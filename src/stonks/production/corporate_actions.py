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

Assumption: the lake's bars are current through ``D``. A split whose
ex-date bar has not been ingested yet would be applied to the quantity
while the quote is still the pre-split close.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from datetime import date, datetime, time

from stonks.backtest.corporate_actions import CorporateActionRecord, apply_to_portfolio
from stonks.core.corporate_actions import CorporateAction, CorporateActions, Dividend, Split
from stonks.core.types import Portfolio
from stonks.execution.reconcile import NON_TERMINAL_STATUSES
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
        cursor = state.execute(
            "UPDATE orders SET quantity = quantity * ?,"
            " limit_price = limit_price / ?, updated_at = ?"
            f" WHERE ticker = ? AND status IN ({status_ph}) AND client_id IN ({ids_ph})",
            [split.ratio, split.ratio, now, split.ticker, *NON_TERMINAL_STATUSES, *client_ids],
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

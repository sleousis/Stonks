"""Reconciliation checks of a live portfolio (roadmap 19.5,
``docs/design/live-trading.md`` section 6).

The broker is the source of truth. A check reconciles the portfolio's
orders first (fills booked from executions, order states synced by client
id), then compares what the broker holds with what the ledger says Stonks
owns (``execution/drift.py``) and stores a ``reconcile_reports`` row.

Kinds:

- ``sod`` (open minus 60 minutes): cancels day and opening-auction orders
  still working from an earlier session, then the full comparison.
- ``submit``: the gate a submit window waits for (:func:`submit_gate`).
  Nothing is sent unless it passes.
- ``eod`` (close plus 15 minutes, before the tick decides): also flags
  orders sent today that are not terminal and executions with no
  commission yet.
- ``adhoc``: by hand (``stonks reconcile run``).

What a check does with its finding:

- ``clean`` or ``warn``: carry on. A warning (a stuck order, a missing
  commission, an order still ``unknown``) alerts the owner.
- ``drift`` (a material item): opens the portfolio's ``broker_drift`` halt
  (buys, closes keep working), pauses its auto subscriptions and pushes a
  high-urgency alert. Clearing the halt needs a reason, like every halt.
- ``outage`` (the broker did not answer): a short outage only skips the
  day. Outages on ``outage_pause_after_sessions`` distinct sessions in a
  row pause auto.
- ``fault`` (a wrong account, a refused login, an answer that makes no
  sense): pauses auto at once.
"""

from __future__ import annotations

import json
import secrets
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import date
from typing import TYPE_CHECKING, Any, Literal, cast

from stonks.core.clock import SYSTEM_CLOCK, Clock
from stonks.core.corporate_actions import CorporateActions
from stonks.core.protocols import Broker
from stonks.execution.brokers.base import (
    BrokerError,
    LiveTradingRefusedError,
    OpenOrderSource,
    OrderCanceller,
    OrderStateSource,
)
from stonks.execution.drift import (
    WORKING,
    DriftItem,
    LedgerOrder,
    eod_items,
    order_drift,
    position_drift,
    report_status,
)
from stonks.execution.order_state import IllegalTransitionError, current_state, state_from_status
from stonks.execution.reconcile import (
    ReconcileSummary,
    reconcile_order,
    reconcile_orders,
    startup_reconcile,
)
from stonks.logging import get_logger
from stonks.production.auto_pause import (
    broker_error_reason,
    broker_failure_kind,
    drift_reason,
    pause_portfolio_auto,
)
from stonks.production.halts import halts_enabled, notify_trip, trip_halt
from stonks.production.live.settings import LiveSettings
from stonks.production.ownership import owned_positions
from stonks.store.state import SqliteState

if TYPE_CHECKING:
    from stonks.execution.brokers.ibkr.factory import ClientFactory
    from stonks.execution.brokers.ibkr.settings import IbkrBrokerConfig

__all__ = [
    "CHECK_KINDS",
    "CHECK_STATUSES",
    "CheckKind",
    "CheckResult",
    "CheckStatus",
    "ReconcileReport",
    "get_report",
    "list_reports",
    "reports_enabled",
    "run_check",
    "run_gateway_checks",
    "submit_gate",
]

_log = get_logger("stonks.production.live.checks")

TABLE = "reconcile_reports"
ACTOR = "service:reconcile"
RUNBOOK_LINK = "/health"

CheckKind = Literal["sod", "submit", "eod", "adhoc"]
CheckStatus = Literal["clean", "warn", "drift", "outage", "fault"]
CHECK_KINDS: tuple[str, ...] = ("sod", "submit", "eod", "adhoc")
CHECK_STATUSES: tuple[str, ...] = ("clean", "warn", "drift", "outage", "fault")

Publish = Callable[[Any], Any]


@dataclass(frozen=True)
class ReconcileReport:
    id: str
    portfolio_id: str
    kind: CheckKind
    as_of: date
    taken_at: str
    status: CheckStatus
    #: The unexplained items.
    items: tuple[DriftItem, ...] = ()
    #: Items the check explained or fixed itself.
    explained: tuple[DriftItem, ...] = ()
    #: The owner's own positions and hand-placed orders: never drift.
    external: Mapping[str, Any] = field(default_factory=dict[str, Any])
    summary: Mapping[str, Any] = field(default_factory=dict[str, Any])
    detail: str | None = None
    halt_id: int | None = None
    paused: tuple[str, ...] = ()

    @classmethod
    def from_row(cls, row: Any) -> ReconcileReport:
        return cls(
            id=row["id"],
            portfolio_id=row["portfolio_id"],
            kind=row["kind"],
            as_of=date.fromisoformat(row["as_of"]),
            taken_at=row["taken_at"],
            status=row["status"],
            items=tuple(DriftItem.from_dict(d) for d in json.loads(row["items_json"])),
            explained=tuple(DriftItem.from_dict(d) for d in json.loads(row["explained_json"])),
            external=json.loads(row["external_json"]),
            summary=json.loads(row["summary_json"]),
            detail=row["detail"],
            halt_id=row["halt_id"],
            paused=tuple(json.loads(row["paused_json"])),
        )


@dataclass(frozen=True)
class CheckResult:
    report: ReconcileReport
    #: Whether the ``broker_drift`` halt was opened by this check.
    halt_created: bool = False

    @property
    def status(self) -> CheckStatus:
        return self.report.status

    @property
    def may_submit(self) -> bool:
        """A submit may go ahead: the broker answered, nothing material
        drifted and no order is still ``unknown``."""
        return self.report.status in ("clean", "warn") and not any(
            i.kind == "unresolved_order" for i in self.report.items
        )


@dataclass
class _Inspection:
    items: list[DriftItem]
    explained: list[DriftItem]
    external: dict[str, Any]
    summary: dict[str, Any]


def reports_enabled(state: SqliteState) -> bool:
    rows = state.sql("SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?", [TABLE])
    return bool(rows)


# ---- the check ------------------------------------------------------------------------


def run_check(
    state: SqliteState,
    broker: Broker,
    portfolio_id: str,
    kind: CheckKind,
    *,
    settings: LiveSettings | None = None,
    clock: Clock = SYSTEM_CLOCK,
    as_of: date | None = None,
    actions: CorporateActions | None = None,
    publish: Publish | None = None,
) -> CheckResult:
    """Reconcile ``portfolio_id`` against ``broker``, store the report and
    act on it (module doc). Broker failures never raise: they become an
    ``outage`` or ``fault`` report."""
    settings = settings or LiveSettings()
    day = as_of or clock.now().date()
    report_id = f"rec_{secrets.token_hex(8)}"
    detail: str | None = None
    try:
        found = _inspect(state, broker, portfolio_id, kind, settings, clock, day, actions)
        status: CheckStatus = report_status(found.items)
    except (
        BrokerError,
        LiveTradingRefusedError,
        ConnectionError,
        TimeoutError,
        IllegalTransitionError,
    ) as exc:
        found = _Inspection([], [], {}, {})
        status = "outage" if broker_failure_kind(exc) == "outage" else "fault"
        detail = f"{type(exc).__name__}: {exc}"[:500]
        _log.warning(
            "reconcile.check_failed", portfolio_id=portfolio_id, kind=kind, status=status,
            error=detail,
        )  # fmt: skip
    open_items = [i for i in found.items if not i.explained]
    halt_id: int | None = None
    halt_created = False
    paused: list[str] = []
    if status == "drift":
        halt_id, halt_created = _open_drift_halt(
            state, portfolio_id, kind, report_id, open_items, day, publish
        )
        material = sum(1 for i in open_items if i.material)
        paused = pause_portfolio_auto(
            state, portfolio_id, drift_reason(report_id, material), as_of=day
        )
    elif status == "fault":
        paused = pause_portfolio_auto(
            state, portfolio_id, broker_error_reason(f"{kind} check: {detail}"), as_of=day
        )
    elif status == "outage":
        sessions = _outage_sessions(state, portfolio_id, day)
        found.summary["outage_sessions"] = sessions
        if sessions >= settings.outage_pause_after_sessions:
            why = f"broker unreachable at reconciliation on {sessions} sessions: {detail}"
            paused = pause_portfolio_auto(state, portfolio_id, broker_error_reason(why), as_of=day)
        _alert_outage(state, portfolio_id, kind, day, detail or "", publish)
    elif status == "warn":
        _alert_warnings(state, portfolio_id, kind, day, open_items, publish)
    report = ReconcileReport(
        id=report_id,
        portfolio_id=portfolio_id,
        kind=kind,
        as_of=day,
        taken_at=clock.now().isoformat(timespec="seconds"),
        status=status,
        items=tuple(open_items),
        explained=tuple(found.explained),
        external=found.external,
        summary=found.summary,
        detail=detail,
        halt_id=halt_id,
        paused=tuple(paused),
    )
    _save(state, report)
    _log.info(
        "reconcile.check", report_id=report_id, portfolio_id=portfolio_id, kind=kind,
        status=status, items=len(open_items), halt_id=halt_id, paused=paused,
    )  # fmt: skip
    return CheckResult(report=report, halt_created=halt_created)


def submit_gate(
    state: SqliteState,
    broker: Broker,
    portfolio_id: str,
    *,
    settings: LiveSettings | None = None,
    clock: Clock = SYSTEM_CLOCK,
    actions: CorporateActions | None = None,
    publish: Publish | None = None,
) -> CheckResult:
    """The short reconcile before a submit window sends anything. Send only
    when :attr:`CheckResult.may_submit`."""
    return run_check(
        state,
        broker,
        portfolio_id,
        "submit",
        settings=settings,
        clock=clock,
        actions=actions,
        publish=publish,
    )


def _inspect(
    state: SqliteState,
    broker: Broker,
    portfolio_id: str,
    kind: CheckKind,
    settings: LiveSettings,
    clock: Clock,
    day: date,
    actions: CorporateActions | None,
) -> _Inspection:
    start = startup_reconcile(broker, state, portfolio_id=portfolio_id, clock=clock)
    summary: dict[str, Any] = _summary_dict(start.summary)
    explained: list[DriftItem] = []
    if kind == "sod" and settings.cancel_stale_orders:
        explained = _cancel_stale(state, broker, portfolio_id, day, clock)
    allow_manual = settings.allow_manual_trades
    items, external = _diff(state, broker, portfolio_id, allow_manual, actions)
    if any(i.material for i in items):
        # a fill or a state change may have landed between the reads:
        # reconcile once more and look again before calling it drift
        again = reconcile_orders(broker, state, now=clock.now(), portfolio_id=portfolio_id)
        summary["recheck"] = _summary_dict(again)
        items, external = _diff(state, broker, portfolio_id, allow_manual, actions)
    items += _reconcile_findings(state, start.summary)
    if kind == "eod":
        items += eod_items(
            stuck=_stuck_orders(state, portfolio_id, day),
            missing_commission=_missing_commissions(state, portfolio_id, day),
        )
    return _Inspection(_dedupe(items), explained, external, summary)


def _diff(
    state: SqliteState,
    broker: Broker,
    portfolio_id: str,
    allow_manual: bool,
    actions: CorporateActions | None,
) -> tuple[list[DriftItem], dict[str, Any]]:
    held = broker.fetch_portfolio().positions
    owned = owned_positions(state, portfolio_id, actions)
    items, external_positions = position_drift(owned, held, allow_manual=allow_manual)
    external: dict[str, Any] = {"positions": external_positions, "orders": []}
    if isinstance(broker, OpenOrderSource):
        working = list(broker.open_orders())
        ids = [o.client_id for o in working if o.client_id is not None]
        ledger = _ledger_orders(state, portfolio_id, ids)
        order_items, external_orders = order_drift(ledger, working, allow_manual=allow_manual)
        external["orders"] = [
            {
                "broker_order_id": o.broker_order_id,
                "ticker": o.ticker,
                "side": o.side,
                "quantity": o.quantity,
            }
            for o in external_orders
        ]
    else:
        # a broker that cannot list working orders: only the ledger's own
        # unresolved orders are known
        ledger = _ledger_orders(state, portfolio_id, [])
        order_items, _ = order_drift(ledger, [], allow_manual=True)
        order_items = [i for i in order_items if i.kind == "unresolved_order"]
    return items + order_items, external


def _reconcile_findings(state: SqliteState, summary: ReconcileSummary) -> list[DriftItem]:
    """Executions that name no order of the portfolio, and broker reports the
    order state machine refused."""
    items = [
        DriftItem(
            kind="unknown_execution",
            key=ref,
            ours=None,
            broker=None,
            material=True,
            detail="an execution with a reference of ours that the ledger cannot place",
        )
        for ref in summary.orphan_fills
    ]
    for client_id in summary.failed_orders:
        ours = current_state(state, client_id)
        items.append(
            DriftItem(
                kind="order_state",
                key=client_id,
                ours=ours,
                broker="refused",
                material=True,
                detail="reconciliation could not apply the broker's report to this order",
            )
        )
    return items


def _dedupe(items: Iterable[DriftItem]) -> list[DriftItem]:
    seen: dict[tuple[str, str], DriftItem] = {}
    for item in items:
        seen.setdefault((item.kind, item.key), item)
    return sorted(seen.values(), key=lambda i: (not i.material, i.kind, i.key))


def _summary_dict(summary: ReconcileSummary) -> dict[str, Any]:
    return {
        "orders_checked": summary.orders_checked,
        "orders_updated": summary.orders_updated,
        "fills_booked": summary.fills_inserted,
        "unknown_orders": list(summary.unknown_orders),
        "failed_orders": list(summary.failed_orders),
        "orphan_executions": list(summary.orphan_fills),
    }


# ---- ledger reads -----------------------------------------------------------------------


def _row_state(row: Any) -> Any:
    raw = row["state"] if row["state"] else None
    return raw or state_from_status(str(row["status"]))


def _ledger_orders(
    state: SqliteState, portfolio_id: str, client_ids: Sequence[str]
) -> list[LedgerOrder]:
    """The portfolio's open orders, plus any of ``client_ids`` whatever
    their state (the broker still works them)."""
    rows = state.sql(
        "SELECT client_id, ticker, status, state FROM orders WHERE portfolio_id = ?"
        " AND status IN ('pending', 'partially_filled') ORDER BY created_at, client_id",
        [portfolio_id],
    )
    out = {r["client_id"]: LedgerOrder(r["client_id"], r["ticker"], _row_state(r)) for r in rows}
    missing = [c for c in client_ids if c not in out]
    if missing:
        marks = ",".join("?" for _ in missing)
        for r in state.sql(
            "SELECT client_id, ticker, status, state FROM orders WHERE portfolio_id = ?"
            f" AND client_id IN ({marks})",
            [portfolio_id, *missing],
        ):
            out[r["client_id"]] = LedgerOrder(r["client_id"], r["ticker"], _row_state(r))
    return list(out.values())


def _stuck_orders(state: SqliteState, portfolio_id: str, day: date) -> list[LedgerOrder]:
    """Orders sent on ``day`` that are still working after the close."""
    rows = state.sql(
        "SELECT client_id, ticker, status, state FROM orders WHERE portfolio_id = ?"
        " AND substr(created_at, 1, 10) = ? AND status IN ('pending', 'partially_filled')"
        " ORDER BY created_at, client_id",
        [portfolio_id, day.isoformat()],
    )
    orders = [LedgerOrder(r["client_id"], r["ticker"], _row_state(r)) for r in rows]
    return [o for o in orders if o.state in WORKING]


def _missing_commissions(state: SqliteState, portfolio_id: str, day: date) -> list[str]:
    rows = state.sql(
        "SELECT broker_exec_id FROM fills WHERE portfolio_id = ? AND broker_exec_id IS NOT NULL"
        " AND fee_currency IS NULL AND substr(filled_at, 1, 10) = ? ORDER BY id",
        [portfolio_id, day.isoformat()],
    )
    return [str(r["broker_exec_id"]) for r in rows]


def _cancel_stale(
    state: SqliteState, broker: Broker, portfolio_id: str, day: date, clock: Clock
) -> list[DriftItem]:
    """Cancel day and opening-auction orders still working from an earlier
    session. Returns what was cancelled (explained) or could not be."""
    if not isinstance(broker, OrderCanceller):
        return []
    rows = state.sql(
        "SELECT client_id, ticker, status, state, created_at FROM orders WHERE portfolio_id = ?"
        " AND status IN ('pending', 'partially_filled') AND substr(created_at, 1, 10) < ?"
        " AND COALESCE(time_in_force, 'day') <> 'gtc' ORDER BY created_at, client_id",
        [portfolio_id, day.isoformat()],
    )
    out: list[DriftItem] = []
    for r in rows:
        current = _row_state(r)
        if current not in WORKING:
            continue
        client_id = r["client_id"]
        placed = str(r["created_at"])[:10]
        try:
            broker.cancel_order(client_id)
        except BrokerError as exc:
            if broker_failure_kind(exc) == "outage":
                raise
            out.append(
                DriftItem(
                    kind="stale_order",
                    key=client_id,
                    ours=current,
                    broker=None,
                    material=False,
                    detail=f"still working from {placed}, and the cancel failed: {exc}",
                )
            )
            continue
        if isinstance(broker, OrderStateSource):
            reconcile_order(broker, state, client_id, now=clock.now(), book_fill=False)
        out.append(
            DriftItem(
                kind="stale_order",
                key=client_id,
                ours=current,
                broker=current_state(state, client_id),
                material=False,
                explained=True,
                detail=f"still working from {placed}: cancelled at the start of the day",
            )
        )
    return out


def _outage_sessions(state: SqliteState, portfolio_id: str, day: date) -> int:
    """Distinct sessions with an outage in the portfolio's latest run of
    outage reports, today's included."""
    days = {day}
    for r in state.sql(
        f"SELECT status, as_of FROM {TABLE} WHERE portfolio_id = ?"
        " ORDER BY taken_at DESC, rowid DESC",
        [portfolio_id],
    ):
        if r["status"] != "outage":
            break
        days.add(date.fromisoformat(r["as_of"]))
    return len(days)


# ---- actions -----------------------------------------------------------------------------


def _open_drift_halt(
    state: SqliteState,
    portfolio_id: str,
    kind: CheckKind,
    report_id: str,
    items: Sequence[DriftItem],
    day: date,
    publish: Publish | None,
) -> tuple[int | None, bool]:
    if not halts_enabled(state):
        return None, False
    material = [i for i in items if i.material]
    named = "; ".join(f"{i.kind} {i.key}" for i in material[:5])
    more = f" and {len(material) - 5} more" if len(material) > 5 else ""
    reason = (
        f"reconcile {kind} report {report_id}: {len(material)} unexplained item(s): {named}{more}"
    )
    halt, created = trip_halt(
        state, "broker_drift", reason=reason, actor=ACTOR, portfolio_id=portfolio_id, on=day
    )
    if created:
        notify_trip(state, halt, publish)
    return halt.id, created


def _send(state: SqliteState, publish: Publish | None, event: Any) -> None:
    from stonks.notify.router import configured_router

    try:
        (publish or configured_router(state).publish)(event)
    except Exception as exc:
        _log.error("reconcile.notify_failed", error=str(exc))


def _alert_outage(
    state: SqliteState,
    portfolio_id: str,
    kind: CheckKind,
    day: date,
    detail: str,
    publish: Publish | None,
) -> None:
    from stonks.notify.events import Audience, Event

    event = Event(
        category="risk",
        level="error",
        urgency="high",
        title="Broker unreachable at reconciliation",
        body=f"The {kind} check could not reach the broker ({detail}). No orders go out for "
        "this portfolio today. The next day decides afresh. See the broker outage runbook.",
        audience=Audience.owner_of(portfolio_id),
        dedupe_key=f"reconcile_outage:{portfolio_id}:{day.isoformat()}",
        deep_link=RUNBOOK_LINK,
        portfolio_id=portfolio_id,
    )
    _send(state, publish, event)


def _alert_warnings(
    state: SqliteState,
    portfolio_id: str,
    kind: CheckKind,
    day: date,
    items: Sequence[DriftItem],
    publish: Publish | None,
) -> None:
    from stonks.notify.events import Audience, Event

    stuck = [i for i in items if i.kind == "stuck_order"]
    names = "; ".join(f"{i.kind} {i.key}" for i in items[:5])
    event = Event(
        category="risk",
        level="warning",
        urgency="high" if stuck else "normal",
        title="Stuck order at the close" if stuck else "Reconciliation warning",
        body=f"The {kind} check found {len(items)} item(s) to look at: {names}. "
        "Nothing is halted. See the reconcile report.",
        audience=Audience.owner_of(portfolio_id),
        dedupe_key=f"reconcile_warn:{portfolio_id}:{kind}:{day.isoformat()}",
        deep_link=RUNBOOK_LINK,
        portfolio_id=portfolio_id,
    )
    _send(state, publish, event)


# ---- storage -------------------------------------------------------------------------------


def _save(state: SqliteState, r: ReconcileReport) -> None:
    if not reports_enabled(state):
        return
    state.execute(
        f"INSERT INTO {TABLE} (id, portfolio_id, kind, as_of, taken_at, status, items_json,"
        " explained_json, external_json, summary_json, detail, halt_id, paused_json)"
        " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        [
            r.id,
            r.portfolio_id,
            r.kind,
            r.as_of.isoformat(),
            r.taken_at,
            r.status,
            json.dumps([i.to_dict() for i in r.items]),
            json.dumps([i.to_dict() for i in r.explained]),
            json.dumps(r.external, sort_keys=True),
            json.dumps(r.summary, sort_keys=True),
            r.detail,
            r.halt_id,
            json.dumps(list(r.paused)),
        ],
    )


def get_report(state: SqliteState, report_id: str) -> ReconcileReport | None:
    rows = state.sql(f"SELECT * FROM {TABLE} WHERE id = ?", [report_id])
    return ReconcileReport.from_row(rows[0]) if rows else None


def list_reports(
    state: SqliteState,
    *,
    portfolio_ids: Sequence[str] | None = None,
    limit: int = 50,
) -> list[ReconcileReport]:
    """Newest first, of ``portfolio_ids`` (every portfolio when ``None``)."""
    if not reports_enabled(state):
        return []
    params: list[Any] = []
    where = ""
    if portfolio_ids is not None:
        if not portfolio_ids:
            return []
        where = f" WHERE portfolio_id IN ({','.join('?' for _ in portfolio_ids)})"
        params += list(portfolio_ids)
    rows = state.sql(
        f"SELECT * FROM {TABLE}{where} ORDER BY taken_at DESC, rowid DESC LIMIT ?", [*params, limit]
    )
    return [ReconcileReport.from_row(r) for r in rows]


# ---- every live portfolio -----------------------------------------------------------------


def run_gateway_checks(
    state: SqliteState,
    config: IbkrBrokerConfig,
    kind: CheckKind,
    *,
    settings: LiveSettings | None = None,
    clock: Clock = SYSTEM_CLOCK,
    client_factory: ClientFactory | None = None,
    actions_for: Callable[[Sequence[str]], CorporateActions | None] | None = None,
    publish: Publish | None = None,
    portfolio_ids: Sequence[str] | None = None,
) -> list[CheckResult]:
    """Check every portfolio listed on a gateway in ``[brokers.ibkr.gateways]``
    (only ``portfolio_ids`` when given), through that gateway (the
    ``reconcile`` client id). ``actions_for`` returns the splits for the
    tickers the portfolios own, when the lake can be read."""
    from stonks.execution.brokers.ibkr.factory import connect_ibkr, default_client_factory

    results: list[CheckResult] = []
    for name, gateway in sorted(config.gateways.items()):
        wanted = [
            pid for pid in gateway.portfolios if portfolio_ids is None or pid in portfolio_ids
        ]
        if not wanted:
            continue
        broker = connect_ibkr(
            config,
            gateway=name,
            role="reconcile",
            state=state,
            clock=clock,
            client_factory=client_factory or default_client_factory,
        )
        try:
            for portfolio_id in wanted:
                actions = None
                if actions_for is not None:
                    actions = actions_for(sorted(owned_positions(state, portfolio_id)))
                results.append(
                    run_check(
                        state,
                        broker,
                        portfolio_id,
                        kind,
                        settings=settings,
                        clock=clock,
                        actions=actions,
                        publish=publish,
                    )
                )
        finally:
            broker.close()
    return results


def check_kind(value: str) -> CheckKind:
    if value not in CHECK_KINDS:
        raise ValueError(f"kind must be one of {', '.join(CHECK_KINDS)}, got {value!r}")
    return cast(CheckKind, value)

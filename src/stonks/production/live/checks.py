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
  commission yet. It compares the broker's cash and settled cash change
  since the last end-of-day check with what Stonks can explain, and the
  broker's statement (IBKR Flex, when configured) with Stonks' fills
  (roadmap 19.15).
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
from datetime import UTC, date, datetime
from typing import TYPE_CHECKING, Any, Literal, cast

from stonks.core.clock import SYSTEM_CLOCK, Clock
from stonks.core.corporate_actions import CorporateActions
from stonks.core.protocols import Broker
from stonks.execution.brokers.base import (
    AccountReader,
    BrokerError,
    LiveTradingRefusedError,
    OpenOrderSource,
    OrderCanceller,
    OrderStateSource,
)
from stonks.execution.drift import (
    WORKING,
    BookedFill,
    BrokerStatement,
    CashFlows,
    DriftItem,
    LedgerOrder,
    cash_drift,
    cash_tolerance,
    eod_items,
    order_drift,
    position_drift,
    report_status,
    statement_drift,
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
from stonks.production.live.settings import LiveSettings, ReconcileSettings
from stonks.production.ownership import owned_positions
from stonks.production.rules._account_settings import AccountRulesSettings
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
#: Reads the broker's official statements (IBKR Flex). Raises on failure.
StatementSource = Callable[[], Sequence[BrokerStatement]]


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


@dataclass(frozen=True)
class _Account:
    """What the end-of-day comparisons know about the broker account."""

    account_id: str | None
    #: Every Stonks portfolio that trades in the account.
    portfolios: tuple[str, ...]
    statements: StatementSource | None
    settlement: AccountRulesSettings


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
    statements: StatementSource | None = None,
    account_id: str | None = None,
    account_portfolios: Sequence[str] | None = None,
    settlement: AccountRulesSettings | None = None,
) -> CheckResult:
    """Reconcile ``portfolio_id`` against ``broker``, store the report and
    act on it (module doc). Broker failures never raise: they become an
    ``outage`` or ``fault`` report.

    The end-of-day check also reads ``statements`` (the broker's official
    record, statements of other accounts than ``account_id`` ignored) and
    explains the account's cash with the fills of every portfolio in
    ``account_portfolios`` (``portfolio_id`` alone by default), settled on
    the ``settlement`` cycles."""
    settings = settings or LiveSettings()
    day = as_of or clock.now().date()
    report_id = f"rec_{secrets.token_hex(8)}"
    detail: str | None = None
    account = _Account(
        account_id=account_id,
        portfolios=tuple(account_portfolios or (portfolio_id,)),
        statements=statements,
        settlement=settlement or AccountRulesSettings(),
    )
    try:
        found = _inspect(state, broker, portfolio_id, kind, settings, clock, day, actions, account)
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
        found.summary["drift_streak"] = _drift_streak(state, portfolio_id) + 1
        # ``drift_streak`` is recorded for the stage gate to read. The stage
        # never moves by itself (owner decision): drift halts buys, pauses
        # auto and alerts, and a person demotes the stage if they want to.
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
    account: _Account,
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
        found, notes = _eod_comparisons(
            state, broker, portfolio_id, settings.reconcile, clock, day, actions, account
        )
        items += found
        summary.update(notes)
    return _Inspection(_dedupe(items), explained, external, summary)


# ---- end of day: statement and cash (roadmap 19.15) ---------------------------------------


def _eod_comparisons(
    state: SqliteState,
    broker: Broker,
    portfolio_id: str,
    rs: ReconcileSettings,
    clock: Clock,
    day: date,
    actions: CorporateActions | None,
    account: _Account,
) -> tuple[list[DriftItem], dict[str, Any]]:
    notes: dict[str, Any] = {}
    items: list[DriftItem] = []
    read: list[BrokerStatement] = []
    if account.statements is not None and (rs.compare_statement or rs.compare_cash):
        try:
            read = [
                s
                for s in account.statements()
                if not account.account_id or not s.account_id or s.account_id == account.account_id
            ]
            notes["statement"] = {"status": "ok", "statements": len(read)}
        except Exception as exc:  # optional: a statement outage never fails a check
            error = f"{type(exc).__name__}: {exc}"[:300]
            notes["statement"] = {"status": "failed", "error": error}
            _log.warning("reconcile.statement_failed", portfolio_id=portfolio_id, error=error)
    if rs.compare_statement:
        items += _statement_items(state, portfolio_id, read, rs.commission_tolerance)
    if rs.compare_cash and isinstance(broker, AccountReader):
        cash_items, notes["cash"] = _cash_items(
            state, broker, portfolio_id, rs, clock, day, actions, account, read
        )
        items += cash_items
    return items, notes


def _order_refs(state: SqliteState, portfolio_ids: Sequence[str]) -> set[str]:
    """Every reference the broker may report for the portfolios' orders."""
    marks = ",".join("?" for _ in portfolio_ids)
    refs: set[str] = set()
    for r in state.sql(
        f"SELECT client_id, broker_ref FROM orders WHERE portfolio_id IN ({marks})",
        list(portfolio_ids),
    ):
        refs.add(str(r["client_id"]))
        if r["broker_ref"]:
            refs.add(str(r["broker_ref"]))
    return refs


def _booked_fills(
    state: SqliteState,
    portfolio_ids: Sequence[str],
    start: date | None = None,
    end: date | None = None,
) -> list[BookedFill]:
    """Fills booked from broker executions, filled from ``start`` to ``end``."""
    marks = ",".join("?" for _ in portfolio_ids)
    sql = (
        "SELECT f.broker_exec_id, f.ticker, f.quantity, f.fee, f.fee_currency, o.side"
        " FROM fills f JOIN orders o ON o.client_id = f.order_client_id"
        f" WHERE f.portfolio_id IN ({marks}) AND f.broker_exec_id IS NOT NULL"
    )
    params: list[Any] = list(portfolio_ids)
    if start is not None and end is not None:
        sql += " AND substr(f.filled_at, 1, 10) BETWEEN ? AND ?"
        params += [start.isoformat(), end.isoformat()]
    return [
        BookedFill(
            exec_id=str(r["broker_exec_id"]),
            ticker=str(r["ticker"]),
            quantity=float(r["quantity"]) * (1.0 if r["side"] == "buy" else -1.0),
            # no commission reported yet: nothing to compare
            fee=float(r["fee"] or 0.0) if r["fee_currency"] else None,
            fee_currency=r["fee_currency"],
        )
        for r in state.sql(sql + " ORDER BY f.id", params)
    ]


def _statement_items(
    state: SqliteState,
    portfolio_id: str,
    statements: Sequence[BrokerStatement],
    commission_tolerance: float,
) -> list[DriftItem]:
    """The statements' executions of the portfolio's own orders against
    its booked fills over the same days. Hand trades are left out."""
    if not statements:
        return []
    refs = _order_refs(state, [portfolio_id])
    items: list[DriftItem] = []
    for s in statements:
        if s.from_date is None or s.to_date is None:
            continue
        booked = _booked_fills(state, [portfolio_id], s.from_date, s.to_date)
        ids = {b.exec_id for b in booked}
        mine = [e for e in s.executions if e.exec_id in ids or (e.order_ref or "") in refs]
        items += statement_drift(mine, booked, commission_tolerance=commission_tolerance)
    return items


def _cash_items(
    state: SqliteState,
    broker: AccountReader,
    portfolio_id: str,
    rs: ReconcileSettings,
    clock: Clock,
    day: date,
    actions: CorporateActions | None,
    account: _Account,
    statements: Sequence[BrokerStatement],
) -> tuple[list[DriftItem], dict[str, Any]]:
    """Cash and settled cash: the broker's change since the last end-of-day
    check against what Stonks can explain. The owner's own trades move the
    same cash, so only a statement can explain those."""
    live = broker.fetch_account()
    now = clock.now()
    note: dict[str, Any] = {
        "currency": live.currency,
        "cash": live.cash,
        "settled_cash": live.settled_cash,
        "equity": live.equity,
        "taken_at": now.isoformat(timespec="seconds"),
        "as_of": day.isoformat(),
        "compared": False,
    }
    before = _cash_baseline(state, portfolio_id, day)
    if before is None:
        note["reason"] = "the first end-of-day check: this one is the baseline"
        return [], note
    if before.get("currency") != live.currency:
        note["reason"] = "the account's base currency changed: this one is the new baseline"
        return [], note
    since = _utc(datetime.fromisoformat(str(before["taken_at"])))
    start_day = date.fromisoformat(str(before["as_of"]))
    trades, fees = _fill_cash(state, account.portfolios, since, now)
    settled_trades = _settled_fill_cash(state, account, live.currency, start_day, day)
    owned = {_root(t) for pid in account.portfolios for t in owned_positions(state, pid, actions)}
    refs = _order_refs(state, account.portfolios)
    booked = {b.exec_id for b in _booked_fills(state, account.portfolios)}
    known = _statement_flows(statements, live.currency, start_day, day, owned, refs, booked)
    flows = CashFlows(trades=trades, fees=fees, dividends=known[0], external=known[1])
    settled = CashFlows(trades=settled_trades, dividends=known[2], external=known[3])
    tolerance = cash_tolerance(
        live.equity, minimum=rs.cash_tolerance, fraction=rs.cash_tolerance_fraction
    )
    material = rs.cash_is_drift
    items = cash_drift(
        "cash",
        live.currency,
        float(before["cash"]),
        live.cash,
        flows,
        tolerance=tolerance,
        material=material,
    ) + cash_drift(
        "settled_cash",
        live.currency,
        float(before["settled_cash"]),
        live.settled_cash,
        settled,
        tolerance=tolerance,
        material=material,
    )
    note.update(
        compared=True,
        since=before["taken_at"],
        tolerance=tolerance,
        flows=_flows_dict(flows),
        settled_flows=_flows_dict(settled),
        statement_flows=bool(statements),
        skipped_foreign=known[4],
    )
    return items, note


def _flows_dict(flows: CashFlows) -> dict[str, float]:
    return {
        "trades": flows.trades,
        "fees": flows.fees,
        "dividends": flows.dividends,
        "external": flows.external,
        "total": flows.total,
    }


def _utc(when: datetime) -> datetime:
    return when if when.tzinfo is not None else when.replace(tzinfo=UTC)


def _root(ticker: str) -> str:
    return ticker.rsplit(".", 1)[0].upper() if "." in ticker else ticker.upper()


def _cash_baseline(state: SqliteState, portfolio_id: str, day: date) -> dict[str, Any] | None:
    """The cash read by the portfolio's latest end-of-day check before ``day``."""
    if not reports_enabled(state):
        return None
    for r in state.sql(
        f"SELECT summary_json FROM {TABLE} WHERE portfolio_id = ? AND kind = 'eod'"
        " AND as_of < ? ORDER BY taken_at DESC, rowid DESC LIMIT 30",
        [portfolio_id, day.isoformat()],
    ):
        cash = json.loads(r["summary_json"]).get("cash")
        if isinstance(cash, dict) and "cash" in cash and "taken_at" in cash:
            return cast(dict[str, Any], cash)
    return None


def _fill_cash(
    state: SqliteState, portfolio_ids: Sequence[str], since: datetime, until: datetime
) -> tuple[float, float]:
    """``(trade cash, fees)`` of the portfolios' fills after ``since`` up to
    ``until``. A buy pays, a sale receives, a fee costs."""
    marks = ",".join("?" for _ in portfolio_ids)
    trades = fees = 0.0
    for r in state.sql(
        "SELECT f.quantity, f.price, f.fee, f.filled_at, o.side FROM fills f"
        " JOIN orders o ON o.client_id = f.order_client_id"
        f" WHERE f.portfolio_id IN ({marks}) AND substr(f.filled_at, 1, 10) >= ?",
        [*portfolio_ids, since.date().isoformat()],
    ):
        at = _utc(datetime.fromisoformat(str(r["filled_at"])))
        if not since < at <= until:
            continue
        amount = float(r["quantity"]) * float(r["price"])
        trades += amount if r["side"] == "sell" else -amount
        fees -= float(r["fee"] or 0.0)
    return trades, fees


def _settled_fill_cash(
    state: SqliteState, account: _Account, currency: str, start_day: date, day: date
) -> float:
    """Cash of the portfolios' fills that settled after ``start_day`` up to
    ``day``, fees included."""
    from stonks.accounts.rules.settlement import load_settlements

    total = 0.0
    for pid in account.portfolios:
        for entry in load_settlements(
            state, pid, account.settlement, currency_of=lambda _t: currency, as_of=day
        ):
            if start_day < entry.settle_date <= day:
                total += entry.amount
    return total


def _statement_flows(
    statements: Sequence[BrokerStatement],
    currency: str,
    start_day: date,
    day: date,
    owned: set[str],
    refs: set[str],
    booked: set[str],
) -> tuple[float, float, float, float, int]:
    """``(dividends, external, settled dividends, settled external,
    rows skipped)`` the statements name from ``start_day`` (excluded) to
    ``day``. Dividends are those on Stonks' positions. External flows are
    the owner's own trades and every other cash movement. Stonks' own
    executions are left out: the ledger's fills already count them. Rows in
    another currency than the account's are skipped (no conversion)."""
    dividends = external = settled_dividends = settled_external = 0.0
    skipped = 0
    seen_exec: set[str] = set()
    seen_cash: set[tuple[Any, ...]] = set()

    def inside(when: date | None) -> bool:
        return when is not None and start_day < when <= day

    for s in statements:
        for e in s.executions:
            if e.exec_id in booked or (e.order_ref or "") in refs or e.exec_id in seen_exec:
                continue
            seen_exec.add(e.exec_id)
            if e.currency and e.currency != currency:
                skipped += 1
                continue
            amount = (e.cash or 0.0) - (e.commission or 0.0)
            if inside(e.trade_date):
                external += amount
            if inside(e.settle_date or e.trade_date):
                settled_external += amount
        for c in s.cash:
            key = (c.kind, c.amount, c.date, c.symbol, c.currency)
            if key in seen_cash:
                continue
            seen_cash.add(key)
            if c.currency and c.currency != currency:
                skipped += 1
                continue
            mine = c.kind == "dividend" and (c.symbol or "").upper() in owned
            if inside(c.date):
                if mine:
                    dividends += c.amount
                else:
                    external += c.amount
            if inside(c.settle_date or c.date):
                if mine:
                    settled_dividends += c.amount
                else:
                    settled_external += c.amount
    return dividends, external, settled_dividends, settled_external, skipped


def _drift_streak(state: SqliteState, portfolio_id: str) -> int:
    """Drift reports in a row before this check, the newest first."""
    if not reports_enabled(state):
        return 0
    streak = 0
    for r in state.sql(
        f"SELECT status FROM {TABLE} WHERE portfolio_id = ? ORDER BY taken_at DESC, rowid DESC"
        " LIMIT 100",
        [portfolio_id],
    ):
        if r["status"] != "drift":
            break
        streak += 1
    return streak


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
    statements: StatementSource | None = None,
    settlement: AccountRulesSettings | None = None,
) -> list[CheckResult]:
    """Check every portfolio listed on a gateway in ``[brokers.ibkr.gateways]``
    (only ``portfolio_ids`` when given), through that gateway (the
    ``reconcile`` client id). ``actions_for`` returns the splits for the
    tickers the portfolios own, when the lake can be read. ``statements``
    defaults to the Flex statement when Flex is configured."""
    from stonks.execution.brokers.ibkr.factory import connect_ibkr, default_client_factory

    if statements is None and kind == "eod":
        from stonks.execution.brokers.ibkr.flex import FlexClient
        from stonks.execution.brokers.ibkr.statements import statement_source

        flex = FlexClient.from_env(config.flex)
        statements = statement_source(flex) if flex is not None else None
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
                        statements=statements,
                        account_id=gateway.account_id,
                        account_portfolios=gateway.portfolios,
                        settlement=settlement,
                    )
                )
        finally:
            broker.close()
    return results


def check_kind(value: str) -> CheckKind:
    if value not in CHECK_KINDS:
        raise ValueError(f"kind must be one of {', '.join(CHECK_KINDS)}, got {value!r}")
    return cast(CheckKind, value)

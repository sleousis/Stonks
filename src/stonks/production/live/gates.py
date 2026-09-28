"""Daily gate metrics and gate reports of the live stages (roadmap 19.9).

**Gate days.** After each session the ``live_gate_days`` job writes one row
per portfolio in ``broker_paper`` or higher (:func:`record_gate_days`):

- orders sent, filled, rejected by the broker, refused by our own rules
  (not failures), and stuck (not terminal when the metrics were taken);
- fills and fills with no commission booked;
- fill quality: the mean TCA gap (realised shortfall minus the cost
  model's estimate, in bps) of the session's orders;
- the book's return and its model book's return, for tracking error;
- unexplained reconciliation items (drift), read through
  :class:`DriftSource`.

A session is **clean** with no drift, no stuck order, every commission
booked and a rejection rate under ``max_reject_rate``. A week that is not
clean raises an alert to the owner. It never changes the stage or the
allocation (owner decision).

**Gate reports.** :func:`gate_report` checks what a promotion to the next
stage needs (design section 1), always computed at that moment. A check
whose data does not exist yet (no reconcile reports before 19.5, no
drills before 19.11) is ``unavailable``: shown, logged in the report, and
not blocking.
"""

from __future__ import annotations

import json
import math
from collections.abc import Callable, Mapping, Sequence
from dataclasses import asdict, dataclass, field
from datetime import date
from typing import Any, Protocol

import numpy as np

from stonks.core.clock import SYSTEM_CLOCK, Clock, iso_now
from stonks.logging import get_logger
from stonks.production.live.settings import StageGateSettings
from stonks.production.live.stages import (
    STAGES,
    LiveStage,
    get_stage,
    next_stage,
    stage_history,
    stages_enabled,
)
from stonks.store.state import SqliteState

_log = get_logger("stonks.production.live.gates")

TABLE = "live_gate_days"
#: Order states that are still working (a stuck order at the end of day).
_WORKING = ("pending", "submitted", "accepted", "partially_filled", "pending_cancel", "unknown")
_TRADING_DAYS = 252
_BOOTSTRAP_DRAWS = 2000


# ---- the seams: drift and the model book ------------------------------------------


class DriftSource(Protocol):
    """Unexplained reconciliation items of a portfolio for a session, or
    ``None`` when there is no reconcile report to read."""

    def unexplained(self, state: SqliteState, portfolio_id: str, day: date) -> int | None: ...


class ReconcileReportDrift:
    """Reads ``reconcile_reports`` (roadmap 19.5): the unexplained items of
    the session's last end-of-day report, else of its last report of any
    kind. ``None`` when the session has no report (or on a state DB from
    before migration 034)."""

    def unexplained(self, state: SqliteState, portfolio_id: str, day: date) -> int | None:
        if not _table_exists(state, "reconcile_reports"):
            return None
        rows = state.sql(
            "SELECT items_json FROM reconcile_reports WHERE portfolio_id = ?"
            " AND as_of = ?"
            " ORDER BY CASE kind WHEN 'eod' THEN 0 ELSE 1 END, taken_at DESC LIMIT 1",
            [portfolio_id, day.isoformat()],
        )
        if not rows:
            return None
        try:
            items = json.loads(rows[0]["items_json"] or "[]")
        except (TypeError, ValueError):
            return None
        return len(items) if isinstance(items, list) else None


class ModelBook(Protocol):
    """The model book's return for a session (``None``: unknown)."""

    def session_return(self, state: SqliteState, portfolio_id: str, day: date) -> float | None: ...


class ShadowModelBook:
    """The weighted return of the model books (``shadow_portfolio_snapshots``)
    of the portfolio's approve and auto subscriptions. Active strategies
    keep a model book with ``[production] model_books = "all"``."""

    def session_return(self, state: SqliteState, portfolio_id: str, day: date) -> float | None:
        subs = state.sql(
            "SELECT strategy_id, weight FROM subscriptions WHERE portfolio_id = ?"
            " AND enabled = 1 AND mode IN ('approve', 'auto') AND weight > 0",
            [portfolio_id],
        )
        total = 0.0
        weighted = 0.0
        for sub in subs:
            rows = state.sql(
                "SELECT as_of, total_value FROM shadow_portfolio_snapshots"
                " WHERE strategy_id = ? AND as_of <= ? ORDER BY as_of DESC LIMIT 2",
                [sub["strategy_id"], day.isoformat()],
            )
            r = _period_return(rows, day)
            if r is None:
                continue
            weighted += float(sub["weight"]) * r
            total += float(sub["weight"])
        return weighted / total if total > 0 else None


# ---- one session ------------------------------------------------------------------


@dataclass(frozen=True)
class GateDay:
    portfolio_id: str
    session_date: date
    stage: LiveStage
    orders_sent: int = 0
    orders_filled: int = 0
    orders_rejected: int = 0
    orders_refused: int = 0
    stuck_orders: int = 0
    fills: int = 0
    fills_missing_commission: int = 0
    tca_orders: int = 0
    tca_gap_bps: float | None = None
    live_return: float | None = None
    model_return: float | None = None
    drift_items: int | None = None
    clean: bool = True
    computed_at: str = ""

    @property
    def reject_rate(self) -> float:
        return self.orders_rejected / self.orders_sent if self.orders_sent else 0.0

    @property
    def tracking_diff(self) -> float | None:
        if self.live_return is None or self.model_return is None:
            return None
        return self.live_return - self.model_return

    def dirty_reasons(self, settings: StageGateSettings) -> list[str]:
        """Why the session is not clean (empty: clean)."""
        out: list[str] = []
        if self.drift_items:
            out.append(f"{self.drift_items} unexplained drift item(s)")
        if self.stuck_orders:
            out.append(f"{self.stuck_orders} stuck order(s)")
        if self.fills_missing_commission:
            out.append(f"{self.fills_missing_commission} fill(s) with no commission")
        if self.orders_sent and self.reject_rate >= settings.max_reject_rate:
            out.append(f"rejection rate {self.reject_rate:.1%}")
        return out

    def as_dict(self) -> dict[str, Any]:
        out = asdict(self)
        out["session_date"] = self.session_date.isoformat()
        out["reject_rate"] = self.reject_rate
        return out


def compute_gate_day(
    state: SqliteState,
    portfolio_id: str,
    day: date,
    *,
    settings: StageGateSettings | None = None,
    drift: DriftSource | None = None,
    model: ModelBook | None = None,
    stage: LiveStage | None = None,
    clock: Clock = SYSTEM_CLOCK,
) -> GateDay:
    """The gate metrics of ``portfolio_id`` for the session ``day``, read
    from the ledger. Writes nothing."""
    settings = settings or StageGateSettings()
    d = day.isoformat()
    orders = state.sql(
        "SELECT client_id, status, COALESCE(state, status) AS fine FROM orders"
        " WHERE portfolio_id = ? AND substr(created_at, 1, 10) = ?",
        [portfolio_id, d],
    )
    fine = [r["fine"] for r in orders]
    filled = sum(1 for r in orders if r["status"] in ("filled", "partially_filled"))
    fills = state.sql(
        "SELECT broker_exec_id, fee_currency FROM fills WHERE portfolio_id = ?"
        " AND substr(filled_at, 1, 10) = ?",
        [portfolio_id, d],
    )
    missing = sum(1 for f in fills if f["broker_exec_id"] and f["fee_currency"] is None)
    gaps = _tca_gaps(state, portfolio_id, day, day)
    gate = GateDay(
        portfolio_id=portfolio_id,
        session_date=day,
        stage=stage or get_stage(state, portfolio_id),
        orders_sent=len(orders),
        orders_filled=filled,
        orders_rejected=sum(1 for s in fine if s == "rejected"),
        orders_refused=_refused(state, portfolio_id, day),
        stuck_orders=sum(1 for s in fine if s in _WORKING),
        fills=len(fills),
        fills_missing_commission=missing,
        tca_orders=len(gaps),
        tca_gap_bps=float(np.mean(gaps)) if gaps else None,
        live_return=_book_return(state, portfolio_id, day),
        model_return=(model or ShadowModelBook()).session_return(state, portfolio_id, day),
        drift_items=(drift or ReconcileReportDrift()).unexplained(state, portfolio_id, day),
        computed_at=iso_now(clock),
    )
    return _with_clean(gate, settings)


def record_gate_day(state: SqliteState, gate: GateDay) -> None:
    """Upsert one session's row (a rerun replaces it)."""
    row = gate.as_dict()
    row.pop("reject_rate")
    row["clean"] = int(gate.clean)
    cols = list(row)
    state.execute(
        f"INSERT INTO {TABLE} ({', '.join(cols)}) VALUES ({', '.join('?' for _ in cols)})"
        " ON CONFLICT (portfolio_id, session_date) DO UPDATE SET "
        + ", ".join(
            f"{c} = excluded.{c}" for c in cols if c not in ("portfolio_id", "session_date")
        ),
        [row[c] for c in cols],
    )


def gate_days(
    state: SqliteState,
    portfolio_id: str,
    *,
    stage: LiveStage | None = None,
    since: date | None = None,
    limit: int | None = None,
) -> list[GateDay]:
    """Stored sessions, oldest first (the newest ``limit`` when given)."""
    if not stages_enabled(state):
        return []
    clauses = ["portfolio_id = ?"]
    params: list[Any] = [portfolio_id]
    if stage is not None:
        clauses.append("stage = ?")
        params.append(stage)
    if since is not None:
        clauses.append("session_date >= ?")
        params.append(since.isoformat())
    sql = f"SELECT * FROM {TABLE} WHERE {' AND '.join(clauses)} ORDER BY session_date DESC"
    if limit is not None:
        sql += " LIMIT ?"
        params.append(limit)
    rows = state.sql(sql, params)
    return [_gate_day(r) for r in reversed(rows)]


def live_portfolios(state: SqliteState) -> list[str]:
    """Active portfolios in ``broker_paper`` or higher."""
    if not stages_enabled(state):
        return []
    rows = state.sql(
        "SELECT id FROM portfolios WHERE status = 'active' AND live_stage <> 'sim_paper'"
        " ORDER BY id"
    )
    return [r["id"] for r in rows]


@dataclass(frozen=True)
class GateDaysRun:
    day: date
    recorded: tuple[GateDay, ...] = ()
    #: Portfolios whose last week was not clean (alerted).
    dirty_weeks: tuple[str, ...] = ()


def record_gate_days(
    state: SqliteState,
    day: date,
    *,
    settings: StageGateSettings | None = None,
    drift: DriftSource | None = None,
    model: ModelBook | None = None,
    alert: Callable[[str, Sequence[GateDay]], None] | None = None,
    clock: Clock = SYSTEM_CLOCK,
) -> GateDaysRun:
    """Compute and store ``day`` for every live portfolio, then alert the
    owner of each one whose last week was not clean. Only alerts: the
    stage and the allocation stay as the owner set them."""
    settings = settings or StageGateSettings()
    recorded: list[GateDay] = []
    dirty: list[str] = []
    for portfolio_id in live_portfolios(state):
        try:
            gate = compute_gate_day(
                state, portfolio_id, day, settings=settings, drift=drift, model=model, clock=clock
            )
            with state.transaction():
                record_gate_day(state, gate)
        except Exception as exc:  # one portfolio never stops the others
            _log.error("live.gate_day_failed", portfolio_id=portfolio_id, error=str(exc))
            continue
        recorded.append(gate)
        week = gate_days(state, portfolio_id, stage=gate.stage, limit=settings.week_sessions)
        if len(week) >= settings.week_sessions and not all(g.clean for g in week):
            dirty.append(portfolio_id)
            (alert or notify_dirty_week)(portfolio_id, week)
    return GateDaysRun(day=day, recorded=tuple(recorded), dirty_weeks=tuple(dirty))


def notify_dirty_week(portfolio_id: str, week: Sequence[GateDay]) -> None:
    """The default alert: a log line. The job passes :func:`owner_alert`."""
    _log.warning(
        "live.dirty_week",
        portfolio_id=portfolio_id,
        sessions=[g.session_date.isoformat() for g in week],
    )


def owner_alert(state: SqliteState) -> Callable[[str, Sequence[GateDay]], None]:
    """A normal-urgency push to the owner: which portfolio had a week that
    was not clean. No amounts or tickers leave the server."""
    from stonks.notify.events import Audience, Event
    from stonks.notify.router import configured_router

    def send(portfolio_id: str, week: Sequence[GateDay]) -> None:
        rows = state.sql("SELECT name FROM portfolios WHERE id = ?", [portfolio_id])
        name = rows[0]["name"] if rows else portfolio_id
        dirty = sum(1 for g in week if not g.clean)
        last = week[-1].session_date.isoformat() if week else ""
        try:
            configured_router(state).publish(
                Event(
                    category="risk",
                    level="warning",
                    urgency="normal",
                    title="Live week not clean",
                    body=f"{dirty} of the last {len(week)} sessions in {name} were not clean",
                    audience=Audience.owner_of(portfolio_id),
                    dedupe_key=f"live_week:{portfolio_id}:{last}",
                    deep_link=f"/profile/live/{portfolio_id}",
                    portfolio_id=portfolio_id,
                )
            )
        except Exception as exc:
            _log.error("live.dirty_week_notify_failed", portfolio_id=portfolio_id, error=str(exc))

    return send


# ---- gate reports -----------------------------------------------------------------


@dataclass(frozen=True)
class GateCheck:
    name: str
    #: ``None``: the data does not exist yet (not blocking).
    passed: bool | None
    detail: str
    value: Any = None
    required: Any = None


@dataclass(frozen=True)
class GateFacts:
    """What the service layer knows and the ledger does not: whether the
    portfolio trades at a broker, and its allocation and account profile."""

    broker_linked: bool = False
    allocation_set: bool = False
    profile_set: bool = False


@dataclass(frozen=True)
class GateInput:
    """What every gate reads."""

    state: SqliteState
    portfolio_id: str
    metrics: Mapping[str, Any]
    facts: GateFacts
    settings: StageGateSettings
    today: date


@dataclass(frozen=True)
class GateReport:
    portfolio_id: str
    from_stage: LiveStage
    #: The stage a promotion would lead to (``None`` at the top).
    target: LiveStage | None
    passed: bool
    checks: tuple[GateCheck, ...]
    metrics: Mapping[str, Any] = field(default_factory=dict[str, Any])
    computed_at: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {
            "portfolio_id": self.portfolio_id,
            "from_stage": self.from_stage,
            "target": self.target,
            "passed": self.passed,
            "checks": [asdict(c) for c in self.checks],
            "metrics": dict(self.metrics),
            "computed_at": self.computed_at,
        }


def stage_since(state: SqliteState, portfolio_id: str) -> date | None:
    """The day the portfolio entered its current stage (``None``: never moved)."""
    history = stage_history(state, portfolio_id, limit=1)
    return date.fromisoformat(history[0].created_at[:10]) if history else None


def stage_metrics(
    state: SqliteState,
    portfolio_id: str,
    stage: LiveStage,
    settings: StageGateSettings,
    *,
    since: date | None = None,
) -> dict[str, Any]:
    """The five numbers over the sessions recorded in ``stage``."""
    days = gate_days(state, portfolio_id, stage=stage, since=since)
    streak = 0
    for g in reversed(days):
        if not g.clean:
            break
        streak += 1
    sent = sum(g.orders_sent for g in days)
    rejected = sum(g.orders_rejected for g in days)
    diffs = [d for g in days if (d := g.tracking_diff) is not None]
    start = days[0].session_date if days else since
    gaps = _tca_gaps(state, portfolio_id, start, days[-1].session_date) if days and start else []
    low, high = _mean_ci(gaps)
    drift_known = [g.drift_items for g in days if g.drift_items is not None]
    return {
        "stage": stage,
        "sessions": len(days),
        "clean_sessions": sum(1 for g in days if g.clean),
        "clean_streak": streak,
        "orders_sent": sent,
        "orders_filled": sum(g.orders_filled for g in days),
        "orders_rejected": rejected,
        "orders_refused": sum(g.orders_refused for g in days),
        "reject_rate": rejected / sent if sent else 0.0,
        "stuck_orders": sum(g.stuck_orders for g in days),
        "fills": sum(g.fills for g in days),
        "fills_missing_commission": sum(g.fills_missing_commission for g in days),
        "drift_items": sum(drift_known) if drift_known else None,
        "drift_sessions_known": len(drift_known),
        "tca_orders": len(gaps),
        "tca_gap_bps": float(np.mean(gaps)) if gaps else None,
        "tca_gap_ci_low": low,
        "tca_gap_ci_high": high,
        "tracking_sessions": len(diffs),
        "tracking_error": (
            float(np.std(diffs, ddof=1) * math.sqrt(_TRADING_DAYS)) if len(diffs) >= 2 else None
        ),
        "first_session": days[0].session_date.isoformat() if days else None,
        "last_session": days[-1].session_date.isoformat() if days else None,
    }


def gate_report(
    state: SqliteState,
    portfolio_id: str,
    *,
    facts: GateFacts | None = None,
    settings: StageGateSettings | None = None,
    clock: Clock = SYSTEM_CLOCK,
) -> GateReport:
    """What a promotion of ``portfolio_id`` to its next stage needs, checked
    now. ``passed`` is true when no check failed (``unavailable`` ones do
    not block)."""
    settings = settings or StageGateSettings()
    facts = facts or GateFacts()
    current = get_stage(state, portfolio_id)
    target = next_stage(current)
    metrics = stage_metrics(
        state, portfolio_id, current, settings, since=stage_since(state, portfolio_id)
    )
    if target is None:
        checks: list[GateCheck] = [
            GateCheck("top_stage", False, "live_scale is the last stage: raise the allocation")
        ]
    else:
        checks = _GATES[target](
            GateInput(state, portfolio_id, metrics, facts, settings, clock.now().date())
        )
    return GateReport(
        portfolio_id=portfolio_id,
        from_stage=current,
        target=target,
        passed=all(c.passed is not False for c in checks),
        checks=tuple(checks),
        metrics=metrics,
        computed_at=iso_now(clock),
    )


def _gate_broker_paper(g: GateInput) -> list[GateCheck]:
    settings, facts = g.settings, g.facts
    subs = g.state.sql(
        "SELECT s.strategy_id, s.mode, s.paper_days_completed, st.status FROM subscriptions s"
        " LEFT JOIN strategies st ON st.id = s.strategy_id"
        " WHERE s.portfolio_id = ? AND s.enabled = 1 AND s.mode IN ('paper', 'approve', 'auto')",
        [g.portfolio_id],
    )
    short = sorted(
        r["strategy_id"] for r in subs if int(r["paper_days_completed"]) < settings.min_paper_days
    )
    inactive = sorted(r["strategy_id"] for r in subs if r["status"] != "active")
    fewest = min((int(r["paper_days_completed"]) for r in subs), default=0)
    return [
        GateCheck(
            "broker_linked",
            facts.broker_linked,
            "the portfolio trades at a broker"
            if facts.broker_linked
            else "link the portfolio to a broker connection first",
        ),
        GateCheck(
            "subscriptions",
            bool(subs),
            f"{len(subs)} subscription(s)" if subs else "no subscription to trade",
            value=len(subs),
            required=1,
        ),
        GateCheck(
            "paper_days",
            bool(subs) and not short,
            "every subscription finished its paper days"
            if subs and not short
            else f"short of paper days: {', '.join(short) or 'none subscribed'}",
            value=fewest,
            required=settings.min_paper_days,
        ),
        GateCheck(
            "strategies_active",
            not inactive,
            "every subscribed strategy is active"
            if not inactive
            else f"not active: {', '.join(inactive)}",
        ),
    ]


def _gate_live_small(g: GateInput) -> list[GateCheck]:
    metrics, facts, settings = g.metrics, g.facts, g.settings
    return [
        _sessions(metrics, settings.min_broker_paper_sessions),
        _clean(metrics, settings.broker_paper_clean_sessions),
        _drift_known(metrics),
        GateCheck(
            "allocation_set",
            facts.allocation_set,
            "the owner set the amount to trade"
            if facts.allocation_set
            else "set the allocation by hand first",
        ),
        GateCheck(
            "account_profile_set",
            facts.profile_set,
            "the account profile is set" if facts.profile_set else "set the account profile first",
        ),
        _drill(g.state, g.portfolio_id, g.today),
        _tracking(metrics, settings),
    ]


def _gate_live_scale(g: GateInput) -> list[GateCheck]:
    metrics, settings = g.metrics, g.settings
    fills = int(metrics["orders_filled"])
    low = metrics["tca_gap_ci_low"]
    return [
        _sessions(metrics, settings.min_live_small_sessions),
        _clean(metrics, settings.live_small_clean_sessions),
        _drift_known(metrics),
        GateCheck(
            "filled_orders",
            fills >= settings.min_live_fills,
            f"{fills} filled live order(s)",
            value=fills,
            required=settings.min_live_fills,
        ),
        GateCheck(
            "tca_gap",
            None if low is None else low <= 0,
            "no TCA gap interval yet (needs two orders with a cost estimate)"
            if low is None
            else (
                f"95% interval {low:.1f} to {metrics['tca_gap_ci_high']:.1f} bps"
                + (" includes zero or sits below it" if low <= 0 else ": the model is too cheap")
            ),
            value=metrics["tca_gap_bps"],
            required="interval low <= 0",
        ),
        _drill(g.state, g.portfolio_id, g.today),
        _tracking(metrics, settings),
    ]


_GATES: dict[LiveStage, Callable[[GateInput], list[GateCheck]]] = {
    "broker_paper": _gate_broker_paper,
    "live_small": _gate_live_small,
    "live_scale": _gate_live_scale,
}
assert set(_GATES) == set(STAGES[1:])


def _sessions(metrics: Mapping[str, Any], need: int) -> GateCheck:
    n = int(metrics["sessions"])
    return GateCheck(
        "sessions",
        n >= need,
        f"{n} session(s) recorded in {metrics['stage']}",
        value=n,
        required=need,
    )


def _clean(metrics: Mapping[str, Any], need: int) -> GateCheck:
    streak = int(metrics["clean_streak"])
    return GateCheck(
        "clean_sessions",
        streak >= need,
        f"the last {streak} session(s) were clean",
        value=streak,
        required=need,
    )


def _drift_known(metrics: Mapping[str, Any]) -> GateCheck:
    known = int(metrics["drift_sessions_known"])
    if known == 0:
        return GateCheck(
            "reconciliation",
            None,
            "unavailable: no reconcile report yet (roadmap 19.5)",
        )
    return GateCheck(
        "reconciliation",
        known >= int(metrics["sessions"]),
        f"reconciled {known} of {metrics['sessions']} session(s)",
        value=known,
        required=metrics["sessions"],
    )


def _drill(state: SqliteState, portfolio_id: str, today: date) -> GateCheck:
    """A passing kill switch drill in the last 30 days.

    TODO(19.11): ``kill_switch_drills`` comes with the drills work package.
    Until then the check is ``unavailable``."""
    if not _table_exists(state, "kill_switch_drills"):
        return GateCheck("kill_switch_drill", None, "unavailable: drills come with roadmap 19.11")
    cols = {r["name"] for r in state.sql("PRAGMA table_info(kill_switch_drills)")}
    if not {"portfolio_id", "passed", "created_at"} <= cols:
        return GateCheck("kill_switch_drill", None, "unavailable: unknown drills table")
    rows = state.sql(
        "SELECT MAX(created_at) AS last FROM kill_switch_drills WHERE portfolio_id = ?"
        " AND passed = 1",
        [portfolio_id],
    )
    last = rows[0]["last"] if rows else None
    if last is None:
        return GateCheck("kill_switch_drill", False, "no passing drill yet")
    age = (today - date.fromisoformat(str(last)[:10])).days
    return GateCheck(
        "kill_switch_drill",
        age <= 30,
        f"last passing drill {age} day(s) ago",
        value=age,
        required=30,
    )


def _tracking(metrics: Mapping[str, Any], settings: StageGateSettings) -> GateCheck:
    te = metrics["tracking_error"]
    limit = settings.max_tracking_error
    if te is None:
        return GateCheck(
            "tracking_error", None, "no model book returns to compare yet", required=limit
        )
    if limit is None:
        return GateCheck("tracking_error", None, f"{te:.2%} a year (reported only)", value=te)
    return GateCheck("tracking_error", te <= limit, f"{te:.2%} a year", value=te, required=limit)


# ---- internals --------------------------------------------------------------------


def _with_clean(gate: GateDay, settings: StageGateSettings) -> GateDay:
    from dataclasses import replace

    return replace(gate, clean=not gate.dirty_reasons(settings))


def _gate_day(row: Any) -> GateDay:
    return GateDay(
        portfolio_id=row["portfolio_id"],
        session_date=date.fromisoformat(row["session_date"]),
        stage=row["stage"],
        orders_sent=int(row["orders_sent"]),
        orders_filled=int(row["orders_filled"]),
        orders_rejected=int(row["orders_rejected"]),
        orders_refused=int(row["orders_refused"]),
        stuck_orders=int(row["stuck_orders"]),
        fills=int(row["fills"]),
        fills_missing_commission=int(row["fills_missing_commission"]),
        tca_orders=int(row["tca_orders"]),
        tca_gap_bps=row["tca_gap_bps"],
        live_return=row["live_return"],
        model_return=row["model_return"],
        drift_items=row["drift_items"],
        clean=bool(row["clean"]),
        computed_at=row["computed_at"],
    )


def _table_exists(state: SqliteState, name: str) -> bool:
    rows = state.sql("SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?", [name])
    return bool(rows)


def _period_return(rows: Sequence[Any], day: date) -> float | None:
    """The return into ``day`` from the two newest rows (newest first)."""
    if len(rows) < 2 or rows[0]["as_of"] != day.isoformat():
        return None
    now, before = float(rows[0]["total_value"]), float(rows[1]["total_value"])
    return now / before - 1.0 if before > 0 else None


def _book_return(state: SqliteState, portfolio_id: str, day: date) -> float | None:
    """The book's return into ``day`` from its last snapshot of each day."""
    rows = state.sql(
        "SELECT as_of, total_value FROM portfolio_snapshots s WHERE portfolio_id = ?"
        " AND as_of <= ? AND id = (SELECT MAX(id) FROM portfolio_snapshots"
        " WHERE portfolio_id = s.portfolio_id AND as_of = s.as_of)"
        " ORDER BY as_of DESC LIMIT 2",
        [portfolio_id, day.isoformat()],
    )
    return _period_return(rows, day)


def _tca_gaps(state: SqliteState, portfolio_id: str, start: date, end: date) -> list[float]:
    """Realised shortfall minus the estimate, per filled order decided in
    ``[start, end]``, in bps."""
    from stonks.production.tca import load_order_tca

    out: list[float] = []
    for row in load_order_tca(state, portfolio_id, since=start, until=end):
        realised, expected = row.shortfall.total_bps, row.shortfall.expected_bps
        if realised is None or expected is None or row.shortfall.filled_notional <= 0:
            continue
        if math.isfinite(realised - expected):
            out.append(realised - expected)
    return out


def _mean_ci(values: Sequence[float]) -> tuple[float | None, float | None]:
    """A bootstrap 95% interval of the mean (fixed seed, so a report is
    reproducible). ``None`` with fewer than two values."""
    if len(values) < 2:
        return None, None
    data = np.asarray(values, dtype=float)
    rng = np.random.default_rng(0)
    means = rng.choice(data, size=(_BOOTSTRAP_DRAWS, data.size), replace=True).mean(axis=1)
    return float(np.percentile(means, 2.5)), float(np.percentile(means, 97.5))


def _refused(state: SqliteState, portfolio_id: str, day: date) -> int:
    """Orders our own rules dropped in the session's ticks (risk
    adjustments to zero). Not failures: counted apart from rejections."""
    rows = state.sql(
        "SELECT summary_json FROM tick_runs WHERE id LIKE ? AND status <> 'running'",
        [f"tick_{day.isoformat()}_%"],
    )
    count = 0
    for r in rows:
        try:
            summary = json.loads(r["summary_json"] or "{}")
        except ValueError:
            continue
        if summary.get("dry_run"):
            continue
        book = summary.get("portfolios", {}).get(portfolio_id)
        if book is None and summary.get("portfolio_id") == portfolio_id:
            book = summary
        for adj in (book or {}).get("risk_adjustments", []):
            if isinstance(adj, dict) and float(adj.get("adjusted_quantity") or 0) <= 0:
                count += 1
    return count

"""The paper soak report (roadmap 19.11, ``docs/design/live-trading.md``
section 8, "Paper soak").

Stage 1 of going live is a soak: a broker portfolio trades the real
schedule against the broker's paper account for at least 20 trading days.
:func:`soak_report` sums up the last N trading days of that portfolio from
the state DB alone, so it runs offline and never talks to the broker:

- orders by outcome (filled, rejected, cancelled, unknown) and the top
  rejection reasons;
- slippage: each fill against the price the strategy decided at, in basis
  points of cost (positive means we paid more than the decision price);
- fills against the model book: the simulated twin of the portfolio
  (``portfolios.paper_of``) that trades the same decisions at model prices;
- gateway outages and recoveries, from the ``gateway_down`` and
  ``gateway_up`` notifications the ``broker_health`` job sends;
- drift: ``broker_drift`` halts, and ``reconcile_reports`` rows when that
  table exists.

A soak is ``clean`` when it has no finding. Findings are plain sentences.
"""

from __future__ import annotations

import statistics
from collections import Counter
from dataclasses import asdict, dataclass, field
from datetime import UTC, date, datetime
from typing import Any

from stonks.store.state import SqliteState

__all__ = ["DriftSummary", "ModelComparison", "SlippageStats", "SoakReport", "soak_report"]

#: The share of orders the broker may reject before the soak is not clean.
MAX_REJECT_RATE = 0.05
#: Mean slippage against the decision price, in bps, before it is a finding.
MAX_MEAN_SLIPPAGE_BPS = 50.0
#: ``reconcile_reports.status`` values that mean no drift.
_CLEAN_STATUSES = frozenset({"ok", "clean", "matched", "pass", "passed"})
_DAY_COLUMNS = ("as_of", "created_at", "checked_at", "reported_at", "run_at", "day")


@dataclass(frozen=True)
class SlippageStats:
    count: int = 0
    mean_bps: float | None = None
    median_bps: float | None = None
    worst_bps: float | None = None


@dataclass(frozen=True)
class ModelComparison:
    """Broker fills against the model book, per decision day, ticker and side."""

    model_portfolio_id: str
    #: Keys both books filled.
    matched: int
    #: Keys only the broker filled.
    broker_only: int
    #: Keys only the model filled (usually a missed or rejected broker order).
    model_only: int
    #: Sum of absolute quantity differences over matched keys.
    quantity_gap: float
    #: Mean absolute price difference of matched keys, in bps of the model price.
    price_gap_bps: float | None


@dataclass(frozen=True)
class DriftSummary:
    """``reconcile_reports`` rows in the window (roadmap 19.5)."""

    reports: int
    with_drift: int


@dataclass(frozen=True)
class SoakReport:
    portfolio_id: str
    days_requested: int
    days_observed: int
    start: date
    end: date
    orders: int = 0
    filled: int = 0
    partially_filled: int = 0
    rejected: int = 0
    cancelled: int = 0
    #: Orders whose outcome is not known (a link drop during a submit).
    unknown: int = 0
    reject_reasons: dict[str, int] = field(default_factory=dict[str, int])
    slippage: SlippageStats = field(default_factory=SlippageStats)
    vs_model: ModelComparison | None = None
    #: Days a gateway was reported down, and days it came back.
    outages: int = 0
    reconnects: int = 0
    drift_halts: int = 0
    drift: DriftSummary | None = None
    findings: list[str] = field(default_factory=list[str])

    @property
    def reject_rate(self) -> float:
        return self.rejected / self.orders if self.orders else 0.0

    @property
    def clean(self) -> bool:
        return not self.findings

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["start"] = self.start.isoformat()
        data["end"] = self.end.isoformat()
        data["reject_rate"] = self.reject_rate
        data["clean"] = self.clean
        return data


def _tables(state: SqliteState) -> set[str]:
    return {r["name"] for r in state.sql("SELECT name FROM sqlite_master WHERE type = 'table'")}


def _columns(state: SqliteState, table: str) -> set[str]:
    return {r["name"] for r in state.sql(f"SELECT name FROM pragma_table_info('{table}')")}


_ORDER_DAY = "substr(COALESCE(o.decided_at, o.created_at), 1, 10)"


def _window(state: SqliteState, portfolio_id: str, days: int, end: date) -> list[str]:
    rows = state.sql(
        f"SELECT DISTINCT {_ORDER_DAY} AS day FROM orders o"
        f" WHERE o.portfolio_id = ? AND {_ORDER_DAY} <= ? ORDER BY day DESC LIMIT ?",
        [portfolio_id, end.isoformat(), days],
    )
    return sorted(r["day"] for r in rows)


def _orders(state: SqliteState, portfolio_id: str, start: str, end: str) -> list[Any]:
    has_state = "state" in _columns(state, "orders")
    fine = "o.state" if has_state else "NULL"
    return state.sql(
        f"SELECT o.client_id, o.side, o.status, o.status_reason, o.decision_price,"
        f" {fine} AS state FROM orders o"
        f" WHERE o.portfolio_id = ? AND {_ORDER_DAY} BETWEEN ? AND ?",
        [portfolio_id, start, end],
    )


def _fills_by_key(
    state: SqliteState, portfolio_id: str, start: str, end: str
) -> dict[tuple[str, str, str], tuple[float, float]]:
    """(day, ticker, side) -> (filled quantity, average price)."""
    rows = state.sql(
        f"SELECT {_ORDER_DAY} AS day, o.ticker, o.side, SUM(f.quantity) AS qty,"
        " SUM(f.quantity * f.price) AS notional FROM fills f"
        " JOIN orders o ON o.client_id = f.order_client_id"
        f" WHERE o.portfolio_id = ? AND {_ORDER_DAY} BETWEEN ? AND ?"
        " GROUP BY day, o.ticker, o.side",
        [portfolio_id, start, end],
    )
    out: dict[tuple[str, str, str], tuple[float, float]] = {}
    for r in rows:
        qty = float(r["qty"] or 0.0)
        if qty > 0:
            out[(r["day"], r["ticker"], r["side"])] = (qty, float(r["notional"]) / qty)
    return out


def _slippage(state: SqliteState, portfolio_id: str, start: str, end: str) -> SlippageStats:
    rows = state.sql(
        "SELECT o.side, o.decision_price, SUM(f.quantity) AS qty,"
        " SUM(f.quantity * f.price) AS notional FROM orders o"
        " JOIN fills f ON f.order_client_id = o.client_id"
        f" WHERE o.portfolio_id = ? AND {_ORDER_DAY} BETWEEN ? AND ?"
        " AND o.decision_price IS NOT NULL AND o.decision_price > 0"
        " GROUP BY o.client_id",
        [portfolio_id, start, end],
    )
    costs: list[float] = []
    for r in rows:
        qty = float(r["qty"] or 0.0)
        if qty <= 0:
            continue
        avg = float(r["notional"]) / qty
        sign = 1.0 if r["side"] == "buy" else -1.0
        costs.append(sign * (avg / float(r["decision_price"]) - 1.0) * 1e4)
    if not costs:
        return SlippageStats()
    return SlippageStats(
        count=len(costs),
        mean_bps=statistics.fmean(costs),
        median_bps=statistics.median(costs),
        worst_bps=max(costs),
    )


def _model_of(state: SqliteState, portfolio_id: str) -> str | None:
    if "paper_of" not in _columns(state, "portfolios"):
        return None
    rows = state.sql(
        "SELECT id FROM portfolios WHERE paper_of = ? ORDER BY created_at, id LIMIT 1",
        [portfolio_id],
    )
    return rows[0]["id"] if rows else None


def _compare(
    state: SqliteState, portfolio_id: str, model_id: str, start: str, end: str
) -> ModelComparison:
    broker = _fills_by_key(state, portfolio_id, start, end)
    model = _fills_by_key(state, model_id, start, end)
    both = broker.keys() & model.keys()
    gaps = [abs(broker[k][1] / model[k][1] - 1.0) * 1e4 for k in both if model[k][1] > 0]
    return ModelComparison(
        model_portfolio_id=model_id,
        matched=len(both),
        broker_only=len(broker.keys() - model.keys()),
        model_only=len(model.keys() - broker.keys()),
        quantity_gap=sum(abs(broker[k][0] - model[k][0]) for k in both),
        price_gap_bps=statistics.fmean(gaps) if gaps else None,
    )


def _gateway_days(state: SqliteState, prefix: str, start: str, end: str) -> int:
    """Distinct (gateway, day) pairs of ``gateway_down``/``gateway_up``
    notifications in the window. Keys are ``<prefix>:<gateway>:<day>:<audience>``."""
    rows = state.sql(
        "SELECT DISTINCT dedupe_key FROM notification_outbox WHERE dedupe_key LIKE ?",
        [f"{prefix}:%"],
    )
    seen: set[tuple[str, str]] = set()
    for r in rows:
        parts = str(r["dedupe_key"]).split(":")
        if len(parts) >= 3 and start <= parts[2] <= end:
            seen.add((parts[1], parts[2]))
    return len(seen)


def _drift_halts(state: SqliteState, portfolio_id: str, start: str, end: str) -> int:
    rows = state.sql(
        "SELECT COUNT(*) AS n FROM risk_halts WHERE kind = 'broker_drift'"
        " AND (portfolio_id = ? OR scope = 'global')"
        " AND substr(tripped_at, 1, 10) BETWEEN ? AND ?",
        [portfolio_id, start, end],
    )
    return int(rows[0]["n"])


def _reconcile_reports(
    state: SqliteState, portfolio_id: str, start: str, end: str
) -> DriftSummary | None:
    """Rows of ``reconcile_reports`` (roadmap 19.5) in the window, read by
    whichever columns the table has. ``None`` when the table is missing."""
    if "reconcile_reports" not in _tables(state):
        return None
    cols = _columns(state, "reconcile_reports")
    day_col = next((c for c in _DAY_COLUMNS if c in cols), None)
    where: list[str] = []
    params: list[Any] = []
    if "portfolio_id" in cols:
        where.append("portfolio_id = ?")
        params.append(portfolio_id)
    if day_col is not None:
        where.append(f"substr({day_col}, 1, 10) BETWEEN ? AND ?")
        params += [start, end]
    rows = state.sql(
        f"SELECT * FROM reconcile_reports WHERE {' AND '.join(where) or '1 = 1'}", params
    )
    drift = 0
    for r in rows:
        keys = r.keys()
        if "ok" in keys and r["ok"] is not None:
            drift += int(not r["ok"])
        elif "status" in keys and r["status"] is not None:
            drift += int(str(r["status"]).lower() not in _CLEAN_STATUSES)
        elif "drift_count" in keys and r["drift_count"] is not None:
            drift += int(r["drift_count"] > 0)
    return DriftSummary(reports=len(rows), with_drift=drift)


def _findings(report: SoakReport) -> list[str]:
    out: list[str] = []
    if report.days_observed < report.days_requested:
        out.append(f"only {report.days_observed} of {report.days_requested} trading days observed")
    if report.unknown:
        out.append(f"{report.unknown} order(s) with an unknown outcome")
    if report.orders and report.reject_rate > MAX_REJECT_RATE:
        out.append(
            f"{report.rejected} of {report.orders} orders rejected "
            f"({report.reject_rate:.0%}, limit {MAX_REJECT_RATE:.0%})"
        )
    mean = report.slippage.mean_bps
    if mean is not None and mean > MAX_MEAN_SLIPPAGE_BPS:
        out.append(f"mean slippage {mean:.1f} bps (limit {MAX_MEAN_SLIPPAGE_BPS:.0f} bps)")
    vs = report.vs_model
    if vs is not None and (vs.broker_only or vs.model_only or vs.quantity_gap > 1e-9):
        out.append(
            f"fills differ from the model book: {vs.model_only} missing, "
            f"{vs.broker_only} extra, quantity gap {vs.quantity_gap:g}"
        )
    if report.outages > report.reconnects:
        out.append(f"{report.outages} gateway outage day(s), {report.reconnects} recovered")
    if report.drift_halts:
        out.append(f"{report.drift_halts} broker drift halt(s)")
    if report.drift is not None and report.drift.with_drift:
        out.append(f"{report.drift.with_drift} reconcile report(s) with drift")
    return out


def soak_report(
    state: SqliteState,
    *,
    portfolio_id: str,
    days: int = 20,
    end: date | None = None,
    model_portfolio_id: str | None = None,
) -> SoakReport:
    """Sum up the last ``days`` trading days of ``portfolio_id`` up to
    ``end`` (default today). A trading day is a decision day with at least
    one order. The model book is ``model_portfolio_id``, else the
    portfolio's paper twin, else left out."""
    if days < 1:
        raise ValueError("days must be at least 1")
    end = end or datetime.now(UTC).date()
    window = _window(state, portfolio_id, days, end)
    start_s = window[0] if window else end.isoformat()
    end_s = end.isoformat()
    rows = _orders(state, portfolio_id, start_s, end_s)
    statuses = Counter(r["status"] for r in rows)
    reasons = Counter(
        (r["status_reason"] or "no reason") for r in rows if r["status"] == "rejected"
    )
    unknown = sum(1 for r in rows if r["state"] == "unknown")
    model_id = model_portfolio_id or _model_of(state, portfolio_id)
    tables = _tables(state)
    report = SoakReport(
        portfolio_id=portfolio_id,
        days_requested=days,
        days_observed=len(window),
        start=date.fromisoformat(start_s),
        end=end,
        orders=len(rows),
        filled=statuses["filled"],
        partially_filled=statuses["partially_filled"],
        rejected=statuses["rejected"],
        cancelled=statuses["cancelled"],
        unknown=unknown,
        reject_reasons=dict(reasons.most_common(10)),
        slippage=_slippage(state, portfolio_id, start_s, end_s),
        vs_model=_compare(state, portfolio_id, model_id, start_s, end_s) if model_id else None,
        outages=_gateway_days(state, "gateway_down", start_s, end_s)
        if "notification_outbox" in tables
        else 0,
        reconnects=_gateway_days(state, "gateway_up", start_s, end_s)
        if "notification_outbox" in tables
        else 0,
        drift_halts=_drift_halts(state, portfolio_id, start_s, end_s)
        if "risk_halts" in tables
        else 0,
        drift=_reconcile_reports(state, portfolio_id, start_s, end_s),
    )
    return SoakReport(**{**report.__dict__, "findings": _findings(report)})

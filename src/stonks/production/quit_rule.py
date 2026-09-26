"""The quit rule (BL-29; Davey, Kahneman's pre-commitment): "the system is
broken".

After each tick, for every active strategy:

1. the **attributed drawdown since promotion**: each day's P&L of the
   positions the strategy owns (``position_attribution`` shares times the
   quantity held, marked at the daily close) summed over every portfolio,
   as a fraction of the mean capital it had at work. The deepest fall of
   that cumulative P&L from its running peak is the drawdown;
2. the **limit**: the tighter of ``quit_multiple`` (1.5, range 1 to 3) times
   the backtest out-of-sample max drawdown (``oos.max_drawdown_oos``) and
   the Monte Carlo 95th percentile (``mc_trades.p95_max_dd``). Without the
   Monte Carlo report the multiple alone is used; with neither, the
   strategy is skipped;
3. past the limit: an ``error`` notification to the admins and, when
   ``auto_demote`` is on (off by default), the strategy moves to
   ``shadow`` with ``actor="system"`` and the reason (audited in
   ``status_changes``).

``min_eval_days`` (126) is the minimum evaluation horizon: each check
says whether it has passed, so a retire for underperformance before it
can ask for an override.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime
from typing import Any

import pandas as pd
from pydantic import BaseModel, ConfigDict, Field

from stonks.logging import get_logger
from stonks.store.state import SqliteState

__all__ = [
    "AttributionRow",
    "CloseLoader",
    "QuitCheck",
    "QuitRuleSettings",
    "apply_quit_rule",
    "attributed_drawdown",
    "evaluate_quit_rule",
    "quit_limit",
]

_log = get_logger("stonks.production.quit_rule")

SYSTEM_ACTOR = "system"
#: Daily bars loaded at most per strategy (about four years).
MAX_BARS = 1_000

#: ``(portfolio_id, as_of, ticker, quantity, weight_share)``.
AttributionRow = tuple[str, date, str, float, float]
#: ``(tickers, as_of, bars) -> {ticker: frame with a "close" column}``.
CloseLoader = Callable[[Sequence[str], date, int], Mapping[str, pd.DataFrame]]


class QuitRuleSettings(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    quit_multiple: float = Field(1.5, ge=1.0, le=3.0)
    auto_demote: bool = False
    min_eval_days: int = Field(126, ge=0)


@dataclass(frozen=True)
class QuitCheck:
    strategy_id: str
    promoted_on: date
    drawdown: float
    limit: float
    limit_source: str
    breached: bool
    eval_complete: bool
    demoted: bool = False

    def as_dict(self) -> dict[str, Any]:
        return {
            "strategy_id": self.strategy_id,
            "promoted_on": self.promoted_on.isoformat(),
            "drawdown": round(self.drawdown, 6),
            "limit": round(self.limit, 6),
            "limit_source": self.limit_source,
            "breached": self.breached,
            "eval_complete": self.eval_complete,
            "demoted": self.demoted,
        }


def quit_limit(
    reports: Mapping[str, Mapping[str, Any]], settings: QuitRuleSettings
) -> tuple[float, str] | None:
    """``(limit, source)`` from the latest survival metrics per test id, or
    ``None`` when neither drawdown is on record."""
    candidates: list[tuple[float, str]] = []
    oos = _number((reports.get("oos") or {}).get("max_drawdown_oos"))
    if oos is not None and oos != 0:
        candidates.append((abs(oos) * settings.quit_multiple, "oos_multiple"))
    p95 = _number((reports.get("mc_trades") or {}).get("p95_max_dd"))
    if p95 is not None and p95 > 0:
        candidates.append((p95, "mc_trades.p95_max_dd"))
    return min(candidates) if candidates else None


def _number(value: Any) -> float | None:
    try:
        out = float(value)
    except (TypeError, ValueError):
        return None
    return out if out == out else None  # NaN is missing


def attributed_drawdown(
    rows: Sequence[AttributionRow],
    closes: Mapping[str, pd.Series],
    *,
    since: date,
    as_of: date,
) -> float:
    """Deepest fall of the strategy's cumulative attributed P&L from its
    peak (which starts at 0) over the days after ``since`` up to ``as_of``,
    as a fraction of its mean attributed exposure. 0 without holdings."""
    if not rows:
        return 0.0
    # per portfolio: its attribution dates, and the holdings on each
    books: dict[str, dict[date, list[tuple[str, float]]]] = {}
    for pid, day, ticker, qty, share in rows:
        books.setdefault(pid, {}).setdefault(day, []).append((ticker, qty * share))
    days = sorted({d for s in closes.values() for d in s.index.date if since <= d <= as_of})
    frames = {
        t: dict(zip(s.index.date, s.to_numpy(dtype=float), strict=True)) for t, s in closes.items()
    }
    cumulative, peak, deepest = 0.0, 0.0, 0.0
    exposures: list[float] = []
    for prev, day in zip(days, days[1:], strict=False):
        pnl, exposure = 0.0, 0.0
        for dated in books.values():
            known = [d for d in dated if d <= prev]
            if not known:
                continue
            for ticker, owned in dated[max(known)]:
                before, after = frames.get(ticker, {}).get(prev), frames.get(ticker, {}).get(day)
                if before is None or after is None:
                    continue
                pnl += owned * (after - before)
                exposure += abs(owned) * after
        cumulative += pnl
        peak = max(peak, cumulative)
        deepest = max(deepest, peak - cumulative)
        if exposure > 0:
            exposures.append(exposure)
    if not exposures or deepest <= 0:
        return 0.0
    return deepest / (sum(exposures) / len(exposures))


def _promoted_on(state: SqliteState, strategy_id: str) -> date:
    rows = state.sql(
        "SELECT MAX(created_at) AS at FROM status_changes"
        " WHERE strategy_id = ? AND kind = 'status' AND to_status = 'active'",
        [strategy_id],
    )
    at = rows[0]["at"] if rows else None
    if not at:
        at = state.sql("SELECT created_at FROM strategies WHERE id = ?", [strategy_id])[0][0]
    return _utc_day(at)


def _utc_day(value: str) -> date:
    parsed = datetime.fromisoformat(value)
    return (parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)).astimezone(UTC).date()


def _latest_metrics(state: SqliteState, strategy_id: str) -> dict[str, dict[str, Any]]:
    rows = state.sql(
        "SELECT test_id, metrics_json FROM survival_reports WHERE strategy_id = ? ORDER BY id",
        [strategy_id],
    )
    return {r["test_id"]: json.loads(r["metrics_json"] or "{}") for r in rows}


def _attribution(state: SqliteState, strategy_id: str, as_of: date) -> list[AttributionRow]:
    exists = state.sql(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'position_attribution'"
    )
    if not exists:
        return []
    rows = state.sql(
        "SELECT portfolio_id, as_of, ticker, quantity, weight_share FROM position_attribution"
        " WHERE strategy_id = ? AND as_of <= ? ORDER BY as_of",
        [strategy_id, as_of.isoformat()],
    )
    return [
        (
            r["portfolio_id"],
            date.fromisoformat(r["as_of"]),
            r["ticker"],
            float(r["quantity"]),
            float(r["weight_share"]),
        )
        for r in rows
    ]


def evaluate_quit_rule(
    state: SqliteState, closes: CloseLoader, as_of: date, settings: QuitRuleSettings
) -> list[QuitCheck]:
    """One check per active strategy that has a limit and holdings."""
    checks: list[QuitCheck] = []
    active = state.sql("SELECT id FROM strategies WHERE status = 'active' ORDER BY id")
    for (sid,) in active:
        limit = quit_limit(_latest_metrics(state, sid), settings)
        if limit is None:
            _log.info("quit_rule.no_limit", strategy_id=sid)
            continue
        promoted = _promoted_on(state, sid)
        rows = _attribution(state, sid, as_of)
        if not rows:
            continue
        # holdings before promotion still count from the promotion day on
        tickers = sorted({r[2] for r in rows})
        bars = min(max((as_of - promoted).days + 5, 5), MAX_BARS)
        frames = closes(tickers, as_of, bars)
        series = {t: f["close"] for t, f in frames.items() if "close" in f}
        dd = attributed_drawdown(rows, series, since=promoted, as_of=as_of)
        checks.append(
            QuitCheck(
                strategy_id=sid,
                promoted_on=promoted,
                drawdown=dd,
                limit=limit[0],
                limit_source=limit[1],
                breached=dd > limit[0],
                eval_complete=(as_of - promoted).days >= settings.min_eval_days,
            )
        )
    return checks


def apply_quit_rule(
    state: SqliteState,
    closes: CloseLoader,
    as_of: date,
    settings: QuitRuleSettings,
    *,
    registry: Any = None,
    publish: Callable[[Any], Any] | None = None,
) -> list[QuitCheck]:
    """:func:`evaluate_quit_rule`, then alert (and demote) each breach."""
    out: list[QuitCheck] = []
    for check in evaluate_quit_rule(state, closes, as_of, settings):
        if check.breached:
            _log.error("quit_rule.breached", **check.as_dict())
            _alert(state, check, publish)
            if settings.auto_demote:
                check = _demote(check, registry)
        out.append(check)
    return out


def _reason(check: QuitCheck) -> str:
    return (
        f"quit rule: attributed drawdown {check.drawdown:.2%} since "
        f"{check.promoted_on.isoformat()} is past the {check.limit:.2%} limit "
        f"({check.limit_source})"
    )


def _alert(state: SqliteState, check: QuitCheck, publish: Callable[[Any], Any] | None) -> None:
    from stonks.notify.events import Audience, Event
    from stonks.notify.router import NotificationRouter

    event = Event(
        category="risk",
        level="error",
        title=f"Quit rule: {check.strategy_id} is past its drawdown limit",
        body=_reason(check),
        audience=Audience.admins(),
        dedupe_key=f"quit_rule:{check.strategy_id}",
        deep_link="/strategies",
        strategy_id=check.strategy_id,
    )
    try:
        (publish or NotificationRouter(state, {}).publish)(event)
    except Exception as exc:
        _log.error("quit_rule.notify_failed", strategy_id=check.strategy_id, error=str(exc))


def _demote(check: QuitCheck, registry: Any) -> QuitCheck:
    if registry is None:
        _log.warning("quit_rule.demote_skipped", strategy_id=check.strategy_id, why="no registry")
        return check
    try:
        registry.set_status(check.strategy_id, "shadow", actor=SYSTEM_ACTOR, reason=_reason(check))
    except Exception as exc:
        _log.error("quit_rule.demote_failed", strategy_id=check.strategy_id, error=str(exc))
        return check
    _log.warning("quit_rule.demoted", strategy_id=check.strategy_id)
    return QuitCheck(**{**check.__dict__, "demoted": True})

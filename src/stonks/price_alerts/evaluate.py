"""Checking price alert rules (roadmap 20.2).

A rule watches one ticker, or every ticker of one of its owner's
watchlists, for one condition:

- ``crosses_above`` / ``crosses_below``: the price moved through ``level``
  since the last check. The last price each rule saw per ticker is kept
  (``price_alert_state``), so a crossing between two checks is found
  whatever their spacing. The first check compares with the bar before.
- ``moves_pct``: the price moved by at least ``pct`` percent, up or down,
  over the last ``window_days`` calendar days. It fires when that becomes
  true, not on every check while it stays true.

:func:`check` is pure: one rule, one ticker, one new observation. Today the
observations are the latest daily closes in the lake, checked by the
``price_alerts`` scheduler job after the prices are ingested. A live quote
feed can call the same function later with its quotes.

A firing is recorded once per rule, ticker and observation
(``price_alert_events``), then published to the owner through the
notification router, which dedupes by key and applies quiet hours and
channel preferences (push, email, Telegram, webhook). The payload names the
ticker, the condition and the price, nothing about holdings.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from typing import Any, Literal

import pandas as pd

from stonks.core.corporate_actions import Split
from stonks.logging import get_logger
from stonks.notify.events import Audience, Event
from stonks.store.corporate_actions import LakeCorporateActions
from stonks.store.lake import DuckDBLake
from stonks.store.state import SqliteState

_log = get_logger("stonks.price_alerts")

Condition = Literal["crosses_above", "crosses_below", "moves_pct"]
#: Extra calendar days of history loaded beyond the longest window.
_HISTORY_PAD_DAYS = 10


@dataclass(frozen=True)
class AlertRule:
    id: str
    owner_id: str
    condition: Condition
    tickers: tuple[str, ...]
    level: float | None = None
    pct: float | None = None
    window_days: int | None = None
    name: str | None = None


@dataclass(frozen=True)
class Observation:
    """A price seen for a ticker at a time (a daily close today, a quote later)."""

    ticker: str
    observed_at: str  # ISO date or timestamp
    price: float


@dataclass(frozen=True)
class Firing:
    rule_id: str
    owner_id: str
    ticker: str
    observed_at: str
    price: float
    title: str
    detail: str


@dataclass(frozen=True)
class RunSummary:
    rules: int = 0
    checked: int = 0
    fired: int = 0
    published: int = 0
    skipped_no_price: int = 0
    firings: tuple[Firing, ...] = field(default_factory=tuple)

    def as_dict(self) -> dict[str, Any]:
        return {
            "rules": self.rules,
            "checked": self.checked,
            "fired": self.fired,
            "published": self.published,
            "skipped_no_price": self.skipped_no_price,
        }


def check(
    rule: AlertRule,
    observation: Observation,
    *,
    previous_price: float | None,
    history: Sequence[tuple[date, float]] = (),
) -> Firing | None:
    """Whether ``rule`` fires for ``observation``. ``previous_price`` is the
    price the rule last saw for the ticker (crossings). ``history`` holds
    the daily closes up to and including the observation, oldest first
    (``moves_pct``)."""
    price = observation.price
    if rule.condition in ("crosses_above", "crosses_below"):
        level = rule.level
        if level is None or previous_price is None:
            return None
        if rule.condition == "crosses_above":
            fired = previous_price < level <= price
            what = f"crossed above {level:g}"
        else:
            fired = previous_price > level >= price
            what = f"crossed below {level:g}"
        if not fired:
            return None
        return _firing(rule, observation, what, f"was {previous_price:g}, now {price:g}")
    if rule.pct is None or rule.window_days is None:
        return None
    now = _move(history, len(history) - 1, rule.window_days)
    if now is None or abs(now) * 100.0 < rule.pct:
        return None
    before = _move(history, len(history) - 2, rule.window_days)
    if before is not None and abs(before) * 100.0 >= rule.pct:
        return None  # still true since the last bar: it fired already
    direction = "up" if now > 0 else "down"
    return _firing(
        rule,
        observation,
        f"moved {direction} {abs(now) * 100.0:.1f}% in {rule.window_days} days",
        f"now {price:g}, threshold {rule.pct:g}%",
    )


def _move(history: Sequence[tuple[date, float]], end: int, window_days: int) -> float | None:
    """The return from the last close on or before ``window_days`` before
    ``history[end]`` to ``history[end]``; ``None`` without enough history."""
    if end < 1 or end >= len(history):
        return None
    day, price = history[end]
    start = day - timedelta(days=window_days)
    base = None
    for d, p in history[: end + 1]:
        if d <= start:
            base = p
    if base is None or base <= 0:
        return None
    return price / base - 1.0


def _firing(rule: AlertRule, observation: Observation, what: str, detail: str) -> Firing:
    return Firing(
        rule_id=rule.id,
        owner_id=rule.owner_id,
        ticker=observation.ticker,
        observed_at=observation.observed_at,
        price=observation.price,
        title=f"{observation.ticker} {what}",
        detail=(f"{rule.name}: " if rule.name else "") + detail,
    )


# ---- one run over the lake's closes ---------------------------------------------------

Publish = Callable[[Event], Any]


def load_rules(state: SqliteState) -> list[AlertRule]:
    """Every enabled rule of an active person, watchlists resolved to their
    tickers (a missing or empty watchlist gives no ticker)."""
    rows = state.sql(
        "SELECT r.*, w.tickers_json FROM price_alert_rules r"
        " JOIN users u ON u.id = r.owner_id"
        " LEFT JOIN watchlists w ON w.id = r.watchlist_id AND w.owner_id = r.owner_id"
        " WHERE r.enabled = 1 AND u.status = 'active' ORDER BY r.created_at, r.id"
    )
    rules: list[AlertRule] = []
    for r in rows:
        if r["target_kind"] == "ticker":
            tickers: tuple[str, ...] = (r["ticker"],)
        else:
            tickers = tuple(json.loads(r["tickers_json"] or "[]"))
        rules.append(
            AlertRule(
                id=r["id"],
                owner_id=r["owner_id"],
                condition=r["condition"],
                tickers=tickers,
                level=r["level"],
                pct=r["pct"],
                window_days=r["window_days"],
                name=r["name"],
            )
        )
    return rules


def load_history(
    lake: DuckDBLake, tickers: Sequence[str], as_of: date, days: int
) -> dict[str, list[tuple[date, float]]]:
    """Daily closes per ticker over the last ``days`` calendar days up to
    ``as_of``, oldest first."""
    if not tickers:
        return {}
    df = lake.sql(
        "SELECT ticker, date, close FROM prices"
        " WHERE ticker = ANY(?) AND date <= ? AND date >= ? ORDER BY ticker, date",
        [list(tickers), as_of, as_of - timedelta(days=days)],
    )
    out: dict[str, list[tuple[date, float]]] = {}
    for rec in df.to_dict("records"):
        day: date = pd.Timestamp(rec["date"]).date()  # type: ignore[assignment]
        out.setdefault(str(rec["ticker"]), []).append((day, float(rec["close"])))
    return out


def run_price_alerts(
    state: SqliteState,
    lake: DuckDBLake,
    *,
    as_of: date,
    publish: Publish,
    now: datetime | None = None,
) -> RunSummary:
    """Check every enabled rule against the latest close on or before
    ``as_of`` of each of its tickers. Idempotent: an observation a rule
    already saw is skipped, and a firing is recorded and published once."""
    now = now or datetime.now(UTC)
    rules = load_rules(state)
    tickers = sorted({t for r in rules for t in r.tickers})
    longest = max((r.window_days or 0 for r in rules), default=0)
    history = load_history(lake, tickers, as_of, longest + _HISTORY_PAD_DAYS)
    splits = LakeCorporateActions(lake).load(tickers)
    seen = _last_seen(state)
    checked = fired = published = no_price = 0
    firings: list[Firing] = []
    for rule in rules:
        for ticker in rule.tickers:
            bars = history.get(ticker)
            if not bars:
                no_price += 1
                continue
            day, price = bars[-1]
            observation = Observation(ticker=ticker, observed_at=day.isoformat(), price=price)
            last = seen.get((rule.id, ticker))
            if last is not None and last[1] >= observation.observed_at:
                continue  # nothing new since the last check
            checked += 1
            # Earlier closes in today's share terms, so a split is no move.
            factor = _split_factors(splits.for_ticker(ticker), day)
            bars = [(d, c / factor(d)) for d, c in bars]
            if last is not None:
                previous: float | None = last[0] / factor(date.fromisoformat(last[1][:10]))
            else:
                previous = bars[-2][1] if len(bars) > 1 else None
            firing = check(rule, observation, previous_price=previous, history=bars)
            with state.transaction():
                if firing is not None and _record(state, firing, now):
                    fired += 1
                    firings.append(firing)
                    if _publish(publish, firing):
                        published += 1
                _remember(state, rule.id, observation)
    summary = RunSummary(
        rules=len(rules),
        checked=checked,
        fired=fired,
        published=published,
        skipped_no_price=no_price,
        firings=tuple(firings),
    )
    _log.info("price_alerts.run", as_of=as_of.isoformat(), **summary.as_dict())
    return summary


def _split_factors(events: Sequence[object], upto: date) -> Callable[[date], float]:
    """``factor(day)``: the product of split ratios with an ex-date after
    ``day`` and on or before ``upto`` (a raw close on ``day`` divided by it
    is in ``upto``'s share terms)."""
    splits = [e for e in events if isinstance(e, Split) and e.ex_date <= upto]

    def factor(day: date) -> float:
        out = 1.0
        for s in splits:
            if s.ex_date > day:
                out *= s.ratio
        return out

    return factor


def _last_seen(state: SqliteState) -> Mapping[tuple[str, str], tuple[float, str]]:
    rows = state.sql("SELECT rule_id, ticker, last_price, last_observed_at FROM price_alert_state")
    return {
        (r["rule_id"], r["ticker"]): (float(r["last_price"]), r["last_observed_at"]) for r in rows
    }


def _remember(state: SqliteState, rule_id: str, observation: Observation) -> None:
    state.execute(
        "INSERT INTO price_alert_state (rule_id, ticker, last_price, last_observed_at)"
        " VALUES (?, ?, ?, ?) ON CONFLICT (rule_id, ticker) DO UPDATE SET"
        " last_price = excluded.last_price, last_observed_at = excluded.last_observed_at",
        [rule_id, observation.ticker, observation.price, observation.observed_at],
    )


def _record(state: SqliteState, firing: Firing, now: datetime) -> bool:
    """Insert the event; ``False`` when this firing is already recorded."""
    cur = state.execute(
        "INSERT OR IGNORE INTO price_alert_events (rule_id, owner_id, ticker, observed_at,"
        " price, detail, created_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
        [
            firing.rule_id,
            firing.owner_id,
            firing.ticker,
            firing.observed_at,
            firing.price,
            f"{firing.title}. {firing.detail}",
            now.isoformat(timespec="seconds"),
        ],
    )
    return bool(cur.rowcount)


def firing_event(firing: Firing) -> Event:
    """The notification for one firing: the owner only, deduped by rule,
    ticker and observation."""
    return Event(
        category="price_alert",
        title=firing.title,
        body=firing.detail,
        audience=Audience.users(firing.owner_id),
        dedupe_key=f"price:{firing.rule_id}:{firing.ticker}:{firing.observed_at}",
        deep_link="/alerts",
    )


def _publish(publish: Publish, firing: Firing) -> bool:
    try:
        publish(firing_event(firing))
    except Exception as exc:  # alerting is a side channel
        _log.error("price_alerts.publish_failed", rule_id=firing.rule_id, error=str(exc))
        return False
    return True

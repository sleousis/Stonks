"""Scheduled screen alerts (roadmap 23.17).

A saved screen can alert its owner. The ``screen_alerts`` scheduler job
runs every due alert on the day's data and compares the matches with the
names the screen matched last time (``screen_alert_matches``). Names that
newly match go to the owner through the notification router, in the
``screen_alert`` category, so the person's channels, quiet hours and
category switch apply. It only notifies: nothing trades.

- The first run of an alert stores its matches as a baseline and sends
  nothing. Every name would be "new" otherwise.
- ``daily`` alerts run on every run of the job, ``weekly`` ones on their
  weekday only. An alert runs at most once per day (``last_as_of``), so a
  rerun of the job is harmless.
- A screen that fails (too many candidates, a deleted universe) records
  ``last_error`` and the other alerts still run.

The screen itself runs through ``run``, so the scheduler backends and the
API service pass their own way of reaching the lake.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from typing import Any, Literal

from stonks.logging import get_logger
from stonks.notify.events import Audience, Event
from stonks.screener.spec import ScreenSpec
from stonks.store.state import SqliteState

_log = get_logger("stonks.screener.alerts")

Cadence = Literal["daily", "weekly"]
#: Runs one screen on a day and returns the tickers it matched.
Runner = Callable[[ScreenSpec, date], Sequence[str]]
Publish = Callable[[Event], Any]

#: Tickers named in a notification; the rest are counted.
NAMED_IN_EVENT = 5


@dataclass(frozen=True)
class ScreenAlertRule:
    screen_id: str
    owner_id: str
    name: str
    spec: ScreenSpec
    cadence: Cadence
    weekday: int | None
    last_as_of: date | None


@dataclass(frozen=True)
class ScreenAlertFinding:
    """Names a screen newly matched on ``as_of``."""

    screen_id: str
    owner_id: str
    name: str
    as_of: date
    new: tuple[str, ...]
    matched: int


@dataclass(frozen=True)
class ScreenAlertRunSummary:
    alerts: int
    ran: int
    baselines: int
    fired: int
    published: int
    failed: int
    findings: tuple[ScreenAlertFinding, ...] = field(default=())

    def as_dict(self) -> dict[str, int]:
        return {
            "alerts": self.alerts,
            "ran": self.ran,
            "baselines": self.baselines,
            "fired": self.fired,
            "published": self.published,
            "failed": self.failed,
        }


def is_due(rule: ScreenAlertRule, as_of: date) -> bool:
    """True when the alert has not run on ``as_of`` and its cadence says
    it runs that day."""
    if rule.last_as_of is not None and rule.last_as_of >= as_of:
        return False
    if rule.cadence == "weekly":
        return rule.weekday == as_of.weekday()
    return True


def load_alerts(state: SqliteState) -> list[ScreenAlertRule]:
    """Every enabled alert of an active person, with its screen."""
    rows = state.sql(
        "SELECT a.screen_id, a.owner_id, a.cadence, a.weekday, a.last_as_of, s.name,"
        " s.spec_json FROM screen_alerts a JOIN screens s ON s.id = a.screen_id"
        " JOIN users u ON u.id = a.owner_id"
        " WHERE a.enabled = 1 AND u.status = 'active' ORDER BY a.created_at, a.screen_id"
    )
    out: list[ScreenAlertRule] = []
    for r in rows:
        try:
            spec = ScreenSpec.model_validate_json(r["spec_json"])
        except ValueError as exc:  # a metric that no longer exists
            _log.warning("screen_alerts.bad_spec", screen_id=r["screen_id"], error=str(exc))
            continue
        out.append(
            ScreenAlertRule(
                screen_id=r["screen_id"],
                owner_id=r["owner_id"],
                name=r["name"],
                spec=spec,
                cadence=r["cadence"],
                weekday=r["weekday"],
                last_as_of=date.fromisoformat(r["last_as_of"]) if r["last_as_of"] else None,
            )
        )
    return out


def run_screen_alerts(
    state: SqliteState,
    *,
    as_of: date,
    run: Runner,
    publish: Publish,
    now: datetime | None = None,
) -> ScreenAlertRunSummary:
    """Run every due alert on ``as_of`` and notify about names that newly
    match. Idempotent per alert and day."""
    now = now or datetime.now(UTC)
    rules = load_alerts(state)
    ran = baselines = fired = published = failed = 0
    findings: list[ScreenAlertFinding] = []
    for rule in rules:
        if not is_due(rule, as_of):
            continue
        try:
            tickers = sorted({str(t) for t in run(rule.spec, as_of)})
        except Exception as exc:  # one broken screen must not stop the others
            failed += 1
            _record_error(state, rule.screen_id, str(exc) or type(exc).__name__, now)
            _log.warning("screen_alerts.run_failed", screen_id=rule.screen_id, error=str(exc))
            continue
        ran += 1
        with state.transaction():
            previous = {
                r["ticker"]
                for r in state.sql(
                    "SELECT ticker FROM screen_alert_matches WHERE screen_id = ?",
                    [rule.screen_id],
                )
            }
            finding: ScreenAlertFinding | None = None
            if rule.last_as_of is None:
                baselines += 1
            else:
                new = tuple(t for t in tickers if t not in previous)
                if new:
                    finding = ScreenAlertFinding(
                        screen_id=rule.screen_id,
                        owner_id=rule.owner_id,
                        name=rule.name,
                        as_of=as_of,
                        new=new,
                        matched=len(tickers),
                    )
                    if not _record_event(state, finding, now):
                        finding = None
            _replace_matches(state, rule.screen_id, previous, tickers, as_of)
            state.execute(
                "UPDATE screen_alerts SET last_as_of = ?, last_error = NULL, updated_at = ?"
                " WHERE screen_id = ?",
                [as_of.isoformat(), _iso(now), rule.screen_id],
            )
        if finding is not None:
            fired += 1
            findings.append(finding)
            if _publish(publish, finding):
                published += 1
    summary = ScreenAlertRunSummary(
        alerts=len(rules),
        ran=ran,
        baselines=baselines,
        fired=fired,
        published=published,
        failed=failed,
        findings=tuple(findings),
    )
    _log.info("screen_alerts.run", as_of=as_of.isoformat(), **summary.as_dict())
    return summary


def finding_event(finding: ScreenAlertFinding) -> Event:
    """The notification: the owner only, one per screen and day."""
    count = len(finding.new)
    named = ", ".join(finding.new[:NAMED_IN_EVENT])
    more = count - NAMED_IN_EVENT
    body = f"{named} and {more} more" if more > 0 else named
    noun = "name" if count == 1 else "names"
    return Event(
        category="screen_alert",
        title=f"{count} new {noun} in {finding.name}",
        body=f"{body} now match. {finding.matched} match in all.",
        audience=Audience.users(finding.owner_id),
        dedupe_key=f"screen:{finding.screen_id}:{finding.as_of.isoformat()}",
        deep_link=f"/screener?screen={finding.screen_id}",
    )


def _replace_matches(
    state: SqliteState, screen_id: str, previous: set[str], tickers: list[str], as_of: date
) -> None:
    current = set(tickers)
    for gone in sorted(previous - current):
        state.execute(
            "DELETE FROM screen_alert_matches WHERE screen_id = ? AND ticker = ?",
            [screen_id, gone],
        )
    for ticker in tickers:
        if ticker not in previous:
            state.execute(
                "INSERT INTO screen_alert_matches (screen_id, ticker, since) VALUES (?, ?, ?)",
                [screen_id, ticker, as_of.isoformat()],
            )


def _record_event(state: SqliteState, finding: ScreenAlertFinding, now: datetime) -> bool:
    cur = state.execute(
        "INSERT OR IGNORE INTO screen_alert_events (screen_id, owner_id, as_of, tickers_json,"
        " matched, created_at) VALUES (?, ?, ?, ?, ?, ?)",
        [
            finding.screen_id,
            finding.owner_id,
            finding.as_of.isoformat(),
            json.dumps(list(finding.new)),
            finding.matched,
            _iso(now),
        ],
    )
    return bool(cur.rowcount)


def _record_error(state: SqliteState, screen_id: str, error: str, now: datetime) -> None:
    state.execute(
        "UPDATE screen_alerts SET last_error = ?, updated_at = ? WHERE screen_id = ?",
        [error[:500], _iso(now), screen_id],
    )


def _publish(publish: Publish, finding: ScreenAlertFinding) -> bool:
    try:
        publish(finding_event(finding))
    except Exception as exc:  # alerting is a side channel
        _log.error("screen_alerts.publish_failed", screen_id=finding.screen_id, error=str(exc))
        return False
    return True


def _iso(ts: datetime) -> str:
    return ts.isoformat(timespec="seconds")

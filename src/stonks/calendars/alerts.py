"""Upcoming-event alerts (roadmap 20.7).

:func:`check_event_alerts` runs after each calendar refresh: for every
active person it finds the events of each registered
:class:`~stonks.calendars.alert_kinds.base.EventAlertKind` on the tickers
they hold or watch (and, for economic releases, in the countries they
follow at the importance they chose, roadmap 20.9), and sends one notification per event in the
``event_alert`` category. A person who turned a kind's switch off
(:class:`~stonks.notify.prefs.EventAlertPrefStore`, by the kind's
``topic``) gets none of that kind. The dedupe key holds the kind, ticker
and date, so a second refresh the same day sends nothing new.

Hook for the price alert engine (roadmap 20.2): :func:`alert_kinds` lists
the kinds and :func:`event_hits` evaluates one for any tickers, so a rule
store can offer "earnings coming up" and "ex-dividend coming up" next to
price rules and route the hits through its own delivery.
"""

from __future__ import annotations

import importlib
import inspect
import pkgutil
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import date
from functools import cache

from stonks.calendars import alert_kinds as _package
from stonks.calendars.alert_kinds.base import AlertAudience, EventAlertKind, EventHit
from stonks.calendars.store import CalendarStore
from stonks.calendars.tracking import active_people, tracked_tickers
from stonks.logging import get_logger
from stonks.notify.events import Audience, Event
from stonks.notify.prefs import EconomicAlertPrefStore, EventAlertPrefStore
from stonks.notify.router import NotificationRouter
from stonks.store.lake import DuckDBLake

_log = get_logger("stonks.calendars.alerts")


@cache
def _kinds() -> dict[str, EventAlertKind]:
    found: dict[str, EventAlertKind] = {}
    for info in pkgutil.iter_modules(_package.__path__):
        if info.name.startswith("_") or info.name == "base":
            continue
        module = importlib.import_module(f"{_package.__name__}.{info.name}")
        for _, obj in inspect.getmembers(module, inspect.isclass):
            if (
                obj.__module__ == module.__name__
                and issubclass(obj, EventAlertKind)
                and not inspect.isabstract(obj)
            ):
                if obj.kind in found:
                    raise RuntimeError(f"duplicate event alert kind {obj.kind!r}")
                found[obj.kind] = obj()
    return found


def alert_kinds() -> list[EventAlertKind]:
    """Every registered kind, by id."""
    return [k for _, k in sorted(_kinds().items())]


def event_hits(
    kind: str,
    store: CalendarStore,
    tickers: Sequence[str],
    today: date,
    days_ahead: int | None = None,
) -> list[EventHit]:
    """The hits of one kind (``KeyError`` for an unknown kind)."""
    try:
        k = _kinds()[kind]
    except KeyError:
        raise KeyError(
            f"unknown event alert kind {kind!r}; choose one of {sorted(_kinds())}"
        ) from None
    return k.hits(store, tickers, today, k.default_days_ahead if days_ahead is None else days_ahead)


@dataclass
class EventAlertReport:
    #: People with at least one event, sent or not.
    people: int = 0
    hits: int = 0
    sent: int = 0
    #: Hits a person had already been told about.
    repeats: int = 0
    #: Hits not sent because the person turned that kind off.
    muted: int = 0
    by_kind: dict[str, int] = field(default_factory=dict)


def _event(user_id: str, hit: EventHit) -> Event:
    return Event(
        category="event_alert",
        title=hit.title,
        body=hit.body,
        audience=Audience.users(user_id),
        urgency="low",
        dedupe_key=hit.dedupe_key,
        deep_link=hit.deep_link,
    )


def check_event_alerts(
    lake: DuckDBLake,
    router: NotificationRouter,
    today: date,
    *,
    days_ahead: Mapping[str, int] | None = None,
) -> EventAlertReport:
    """Send the upcoming-event notifications for ``today``. ``days_ahead``
    overrides a kind's look-ahead; ``0`` turns a kind off (``-1`` too)."""
    store = CalendarStore(lake)
    report = EventAlertReport()
    overrides = dict(days_ahead or {})
    switches = EventAlertPrefStore(router.state)
    economic = EconomicAlertPrefStore(router.state)
    for user_id in active_people(router.state):
        choices = economic.get(user_id)
        audience = AlertAudience(
            tickers=tuple(tracked_tickers(router.state, user_id)),
            countries=choices.countries,
            min_importance=choices.min_importance,
        )
        wanted = switches.switches(user_id)
        heard = False
        for kind in alert_kinds():
            days = overrides.get(kind.kind, kind.default_days_ahead)
            if days <= 0:
                continue
            for hit in kind.hits_for(store, audience, today, days):
                heard = True
                report.hits += 1
                if not wanted.get(kind.topic, True):
                    report.muted += 1
                    continue
                result = router.publish(_event(user_id, hit))
                if result.notification_ids:
                    report.sent += 1
                    report.by_kind[kind.kind] = report.by_kind.get(kind.kind, 0) + 1
                else:
                    report.repeats += 1
        report.people += int(heard)
    _log.info(
        "calendars.event_alerts",
        people=report.people,
        hits=report.hits,
        sent=report.sent,
        repeats=report.repeats,
        muted=report.muted,
    )
    return report

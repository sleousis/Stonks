"""The seam for upcoming-event alert kinds (roadmap 20.7).

An :class:`EventAlertKind` finds the events of one kind (an earnings
report, an ex-dividend date) that fall on ``today`` to ``today +
days_ahead`` for a set of tickers, as :class:`EventHit` rows. The calendar
refresh sends each hit once per person (the dedupe key holds the event).

A kind that is not about tickers (economic releases) overrides
:meth:`EventAlertKind.hits_for`, which gets the whole
:class:`AlertAudience`: the person's tickers and their economic release
choices.

Adding a kind is one new module in :mod:`stonks.calendars.alert_kinds` with
a subclass; :mod:`stonks.calendars.alerts` discovers it. The price alert
engine (roadmap 20.2) can offer these kinds as rule kinds through
:func:`stonks.calendars.alerts.event_hits`.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date
from typing import ClassVar

from stonks.calendars.countries import FALLBACK_COUNTRY
from stonks.calendars.importance import Importance
from stonks.calendars.store import CalendarStore


@dataclass(frozen=True)
class AlertAudience:
    """Who one check runs for: the tickers they hold or watch, and the
    countries and importance threshold of their economic release alerts."""

    tickers: tuple[str, ...] = ()
    countries: tuple[str, ...] = (FALLBACK_COUNTRY,)
    min_importance: Importance = "high"


@dataclass(frozen=True)
class EventHit:
    kind: str
    ticker: str
    event_date: date
    #: One line, no amounts or holdings (notification payload rule).
    title: str
    body: str
    #: What tells this event apart when ticker and date do not (several
    #: releases of one country on one day). Part of the dedupe key.
    event_id: str | None = None
    #: The console page to open, when not the ticker's calendar.
    link: str | None = None

    @property
    def dedupe_key(self) -> str:
        key = f"event:{self.kind}:{self.ticker}:{self.event_date.isoformat()}"
        return f"{key}:{self.event_id}" if self.event_id else key

    @property
    def deep_link(self) -> str:
        if self.link is not None:
            return self.link
        return f"/calendar?ticker={self.ticker}&date={self.event_date.isoformat()}"


def when(event_date: date, today: date) -> str:
    """``today``, ``tomorrow`` or ``on 2026-10-29``."""
    days = (event_date - today).days
    if days == 0:
        return "today"
    if days == 1:
        return "tomorrow"
    return f"on {event_date.isoformat()}"


class EventAlertKind(ABC):
    #: Stable id, used in rule kinds and dedupe keys.
    kind: ClassVar[str]
    #: Plain words for the console.
    label: ClassVar[str]
    #: The person's switch this kind follows, a key of
    #: :data:`stonks.notify.prefs.EVENT_ALERT_TOPICS` (earnings, dividends,
    #: economic). Several kinds may share one switch.
    topic: ClassVar[str]
    #: How far ahead to look when a rule does not say.
    default_days_ahead: ClassVar[int]

    @abstractmethod
    def hits(
        self, store: CalendarStore, tickers: Sequence[str], today: date, days_ahead: int
    ) -> list[EventHit]:
        """Events of this kind for ``tickers`` on ``today`` to
        ``today + days_ahead``."""

    def hits_for(
        self, store: CalendarStore, audience: AlertAudience, today: date, days_ahead: int
    ) -> list[EventHit]:
        """The hits for one person. Ticker kinds read the person's tickers,
        and a person who follows none gets none."""
        if not audience.tickers:
            return []
        return self.hits(store, audience.tickers, today, days_ahead)

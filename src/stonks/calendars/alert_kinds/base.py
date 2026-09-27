"""The seam for upcoming-event alert kinds (roadmap 20.7).

An :class:`EventAlertKind` finds the events of one kind (an earnings
report, an ex-dividend date) that fall on ``today`` to ``today +
days_ahead`` for a set of tickers, as :class:`EventHit` rows. The calendar
refresh sends each hit once per person (the dedupe key holds the event).

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

from stonks.calendars.store import CalendarStore


@dataclass(frozen=True)
class EventHit:
    kind: str
    ticker: str
    event_date: date
    #: One line, no amounts or holdings (notification payload rule).
    title: str
    body: str

    @property
    def dedupe_key(self) -> str:
        return f"event:{self.kind}:{self.ticker}:{self.event_date.isoformat()}"

    @property
    def deep_link(self) -> str:
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
    #: How far ahead to look when a rule does not say.
    default_days_ahead: ClassVar[int]

    @abstractmethod
    def hits(
        self, store: CalendarStore, tickers: Sequence[str], today: date, days_ahead: int
    ) -> list[EventHit]:
        """Events of this kind for ``tickers`` on ``today`` to
        ``today + days_ahead``."""

"""Alert before an economic release (CPI, payrolls, a rate decision).

Releases are market wide, so this kind reads the person's economic
choices instead of their tickers: the countries they follow and the
lowest importance that alerts them (roadmap 20.9). One release that the
vendor lists with several comparisons (month on month and year on year)
is one alert.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import date, timedelta

from stonks.calendars.alert_kinds.base import AlertAudience, EventAlertKind, EventHit, when
from stonks.calendars.countries import COUNTRY_LABELS
from stonks.calendars.importance import at_least
from stonks.calendars.store import CalendarStore

#: The longest release name the dedupe key carries (the key is capped).
_KEY_NAME_MAX = 120


class EconomicRelease(EventAlertKind):
    kind = "economic_release"
    label = "Economic release coming up"
    topic = "economic"
    default_days_ahead = 1

    def hits(
        self, store: CalendarStore, tickers: Sequence[str], today: date, days_ahead: int
    ) -> list[EventHit]:
        """Releases are not about tickers: the default choices apply (US,
        high importance)."""
        return self.hits_for(store, AlertAudience(tickers=tuple(tickers)), today, days_ahead)

    def hits_for(
        self, store: CalendarStore, audience: AlertAudience, today: date, days_ahead: int
    ) -> list[EventHit]:
        if not audience.countries:
            return []
        out: list[EventHit] = []
        seen: set[tuple[str, str, str]] = set()
        events = store.economic(
            today, today + timedelta(days=days_ahead), countries=audience.countries
        )
        for e in events:
            if not at_least(e.importance, audience.min_importance):
                continue
            stamp = e.event_time.strftime("%H:%M")
            key = (e.country, e.event_type, e.event_time.isoformat())
            if key in seen:
                continue
            seen.add(key)
            day = e.event_time.date()
            where = COUNTRY_LABELS.get(e.country, e.country)
            out.append(
                EventHit(
                    kind=self.kind,
                    ticker=e.country,
                    event_date=day,
                    title=f"{e.country}: {e.event_type} {when(day, today)}",
                    body=(
                        f"{where} {e.event_type} is due {when(day, today)} at {stamp} UTC."
                        f" {e.importance.capitalize()} importance."
                    ),
                    event_id=f"{e.event_type[:_KEY_NAME_MAX]}@{stamp}",
                    link=f"/calendar?country={e.country}&date={day.isoformat()}",
                )
            )
        return out

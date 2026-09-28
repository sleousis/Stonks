"""Alert before a company reports earnings."""

from __future__ import annotations

from collections.abc import Sequence
from datetime import date, timedelta

from stonks.calendars.alert_kinds.base import EventAlertKind, EventHit, when
from stonks.calendars.store import CalendarStore

_TIMING = {"before": "before the open", "after": "after the close", "during": "during the session"}


class EarningsUpcoming(EventAlertKind):
    kind = "earnings_upcoming"
    label = "Earnings coming up"
    topic = "earnings"
    default_days_ahead = 2

    def hits(
        self, store: CalendarStore, tickers: Sequence[str], today: date, days_ahead: int
    ) -> list[EventHit]:
        out = []
        for e in store.earnings(today, today + timedelta(days=days_ahead), tickers=tickers):
            timing = _TIMING.get(e.before_after_market or "")
            body = f"{e.name or e.ticker} reports {when(e.report_date, today)}"
            body += f", {timing}." if timing else "."
            out.append(
                EventHit(
                    kind=self.kind,
                    ticker=e.ticker,
                    event_date=e.report_date,
                    title=f"{e.ticker}: earnings {when(e.report_date, today)}",
                    body=body,
                )
            )
        return out

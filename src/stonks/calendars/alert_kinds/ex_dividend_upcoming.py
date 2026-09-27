"""Alert before an ex-dividend date (buy before it to receive the dividend)."""

from __future__ import annotations

from collections.abc import Sequence
from datetime import date, timedelta

from stonks.calendars.alert_kinds.base import EventAlertKind, EventHit, when
from stonks.calendars.store import CalendarStore


class ExDividendUpcoming(EventAlertKind):
    kind = "ex_dividend_upcoming"
    label = "Ex-dividend date coming up"
    default_days_ahead = 1

    def hits(
        self, store: CalendarStore, tickers: Sequence[str], today: date, days_ahead: int
    ) -> list[EventHit]:
        return [
            EventHit(
                kind=self.kind,
                ticker=d.ticker,
                event_date=d.ex_date,
                title=f"{d.ticker}: ex-dividend {when(d.ex_date, today)}",
                body=f"{d.name or d.ticker} goes ex-dividend {when(d.ex_date, today)}.",
            )
            for d in store.dividends(today, today + timedelta(days=days_ahead), tickers=tickers)
        ]

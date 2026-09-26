"""Asset-class-aware trading calendars, used to annualize Sharpe.

A ``TradingCalendar`` says how many trading sessions a year has and how
long each one is. Two cover the closed ``AssetClass`` set:

- ``EXCHANGE_SESSIONS`` for ``equity``, ``commodity`` and ``bond``: 252
  sessions of 6.5 hours (US regular hours; futures and bond markets trade
  longer, but vendor intraday bars for them are sparse outside the day
  session, so this is the conservative count).
- ``ALWAYS_OPEN`` for ``crypto``: 365 sessions of 24 hours.

Mixed universes
---------------
The backtest engine marks equity once per timestamp that has a bar for
*any* universe ticker, so the equity curve is sampled on the union of the
tickers' calendars. The 24/7 calendar is a superset of exchange sessions,
so the union is simply the densest calendar present, and
``calendar_for_universe`` returns that one. This is deterministic (it does
not depend on how many bars happen to fall in a short window, which an
empirical bars-per-year estimate would) and exact whenever the data
follows its asset class's calendar.
"""

from __future__ import annotations

import math
from collections.abc import Iterable
from dataclasses import dataclass

from stonks.core.interval import Interval
from stonks.core.types import AssetClass

_SECONDS_PER_DAY = 24 * 3600


@dataclass(frozen=True)
class TradingCalendar:
    name: str
    sessions_per_year: float
    session_seconds: int

    def periods_per_year(self, interval: Interval) -> float:
        """Bars of ``interval`` in one year on this calendar.

        - intraday: ``sessions * max(1, ceil(session / interval))``: a
          6.5 hour session prints 7 hourly bars and 2 four-hour bars (the
          last bar covers the rest of the session, RS-16), and bars longer
          than the session still count once per session
        - day multiples: ``sessions / days``
        - weeks ``52 / n``, months ``12 / n``, years ``1 / n`` on every
          calendar (one bar per calendar period whatever the session)
        """
        unit, amount = interval.unit, interval.amount
        if interval.is_intraday:
            bars = math.ceil(round(self.session_seconds / interval.seconds, 9))
            return self.sessions_per_year * max(1, bars)
        if unit == "d":
            return self.sessions_per_year / amount
        if unit == "w":
            return 52 / amount
        if unit == "mo":
            return 12 / amount
        return 1 / amount


EXCHANGE_SESSIONS = TradingCalendar("exchange_sessions", 252, int(6.5 * 3600))
ALWAYS_OPEN = TradingCalendar("always_open", 365, _SECONDS_PER_DAY)

_BY_ASSET_CLASS: dict[str, TradingCalendar] = {
    "equity": EXCHANGE_SESSIONS,
    "commodity": EXCHANGE_SESSIONS,
    "bond": EXCHANGE_SESSIONS,
    "crypto": ALWAYS_OPEN,
}


def calendar_for(asset_class: AssetClass) -> TradingCalendar:
    """Calendar for one asset class; unknown classes trade exchange sessions."""
    return _BY_ASSET_CLASS.get(asset_class, EXCHANGE_SESSIONS)


def calendar_for_universe(asset_classes: Iterable[AssetClass]) -> TradingCalendar:
    """The densest calendar among ``asset_classes`` (see module docstring);
    ``EXCHANGE_SESSIONS`` for an empty universe."""
    calendars = {calendar_for(a) for a in asset_classes} or {EXCHANGE_SESSIONS}
    return max(calendars, key=lambda c: c.sessions_per_year * c.session_seconds)

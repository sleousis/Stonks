"""Market calendars (roadmap 12.1): when an exchange is open.

Not to be confused with ``backtest.calendar``, which only knows how many
sessions a year an *asset class* has (for annualizing Sharpe). This module
knows the actual sessions of actual exchanges: holidays, early closes and
open/close instants in UTC, so the scheduler can say "30 minutes after the
NYSE close on trading days" and the tick can skip days its whole universe
is closed.

``MarketCalendar`` is the seam. Exchange sessions come from the
``exchange_calendars`` library behind :class:`ExchangeCalendar`; crypto
trades around the clock on :class:`AlwaysOpenCalendar`. The registry maps
calendar names (ISO MIC codes such as ``XNYS``, plus ``24/7``) to
factories, and :data:`EXCHANGE_CALENDARS` maps our canonical exchange codes
(the ticker suffix in ``AAPL.US``, ``VOD.LSE``) to calendar names. Adding
an exchange is one line in that table; adding a calendar source is one
``register_calendar`` call.

Every instant is a timezone-aware UTC ``datetime``; naive datetimes are
rejected, because a naive "22:00" is exactly the DST bug this module
exists to prevent. Session dates are plain ``date`` objects in the
exchange's own local calendar.
"""

from __future__ import annotations

import threading
from abc import ABC, abstractmethod
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta

#: How far ``next_session`` / ``previous_session`` search before giving up.
#: The longest real closures (Chinese New Year, Golden Week) are ~10 days.
_MAX_SEARCH_DAYS = 60


class UnknownCalendarError(KeyError):
    """No calendar is registered (or mapped) under this name or code."""


class CalendarRangeError(ValueError):
    """The date is outside the range the calendar has sessions for."""


@dataclass(frozen=True)
class Session:
    """One trading session: its local date and UTC open/close instants."""

    date: date
    open: datetime
    close: datetime


def ensure_utc(when: datetime) -> datetime:
    if when.tzinfo is None or when.utcoffset() is None:
        raise ValueError(f"expected a timezone-aware datetime, got naive {when!r}")
    return when.astimezone(UTC)


class MarketCalendar(ABC):
    """When a market trades. Subclasses implement ``session``; the rest
    has defaults built on it (override for speed where the source offers
    a vectorized query)."""

    name: str

    @abstractmethod
    def session(self, day: date) -> Session | None:
        """The session on ``day``, or None when the market is closed."""

    def is_trading_day(self, day: date) -> bool:
        return self.session(day) is not None

    def sessions(self, start: date, end: date) -> list[Session]:
        """Sessions with ``start <= date <= end``, in order."""
        out: list[Session] = []
        day = start
        while day <= end:
            s = self.session(day)
            if s is not None:
                out.append(s)
            day += timedelta(days=1)
        return out

    def holidays(self, start: date, end: date) -> list[date]:
        """Days in ``[start, end]`` that are normally trading days (per
        :meth:`is_regular_weekday`) but have no session."""
        out: list[date] = []
        day = start
        while day <= end:
            if self.is_regular_weekday(day) and self.session(day) is None:
                out.append(day)
            day += timedelta(days=1)
        return out

    def is_regular_weekday(self, day: date) -> bool:
        return day.weekday() < 5

    def next_session(self, after: date) -> Session:
        """The first session strictly after ``after``."""
        day = after
        for _ in range(_MAX_SEARCH_DAYS):
            day += timedelta(days=1)
            s = self.session(day)
            if s is not None:
                return s
        raise CalendarRangeError(f"{self.name}: no session within {_MAX_SEARCH_DAYS}d of {after}")

    def previous_session(self, before: date) -> Session:
        """The last session strictly before ``before``."""
        day = before
        for _ in range(_MAX_SEARCH_DAYS):
            day -= timedelta(days=1)
            s = self.session(day)
            if s is not None:
                return s
        raise CalendarRangeError(f"{self.name}: no session within {_MAX_SEARCH_DAYS}d of {before}")

    def next_open(self, after: datetime) -> datetime:
        """The first session open strictly after ``after``."""
        return self._next_edge(ensure_utc(after), lambda s: s.open)

    def next_close(self, after: datetime) -> datetime:
        """The first session close strictly after ``after``."""
        return self._next_edge(ensure_utc(after), lambda s: s.close)

    def is_open_at(self, when: datetime) -> bool:
        when = ensure_utc(when)
        # A session's local date can differ from its UTC date by one day.
        day = when.date()
        for d in (day - timedelta(days=1), day, day + timedelta(days=1)):
            s = self.session(d)
            if s is not None and s.open <= when < s.close:
                return True
        return False

    def _next_edge(self, after: datetime, edge: Callable[[Session], datetime]) -> datetime:
        start = after.date() - timedelta(days=1)
        for s in self.sessions(start, start + timedelta(days=_MAX_SEARCH_DAYS)):
            if edge(s) > after:
                return edge(s)
        raise CalendarRangeError(f"{self.name}: nothing within {_MAX_SEARCH_DAYS}d of {after}")


class AlwaysOpenCalendar(MarketCalendar):
    """Crypto: one session per UTC day, 00:00 to 24:00, every day."""

    name = "24/7"

    def session(self, day: date) -> Session:
        start = datetime(day.year, day.month, day.day, tzinfo=UTC)
        return Session(day, start, start + timedelta(days=1))

    def is_regular_weekday(self, day: date) -> bool:
        return True


class ExchangeCalendar(MarketCalendar):
    """An exchange calendar from the ``exchange_calendars`` library.

    The library's types (pandas Timestamps, its own exceptions) stay in
    here. Its sessions cover a finite window (by default 20 years back to
    one year ahead); dates outside it raise :class:`CalendarRangeError`
    rather than guessing.
    """

    def __init__(self, code: str) -> None:
        import exchange_calendars as xc

        try:
            self._cal = xc.get_calendar(code)
        except xc.errors.InvalidCalendarName as exc:
            raise UnknownCalendarError(code) from exc
        self.name = code
        self._first = self._cal.first_session.date()
        self._last = self._cal.last_session.date()
        self._weekmask = self._cal.weekmask  # e.g. "1111100"

    def _check(self, day: date) -> None:
        if not self._first <= day <= self._last:
            raise CalendarRangeError(
                f"{self.name} has sessions from {self._first} to {self._last}, not {day}"
            )

    def session(self, day: date) -> Session | None:
        import pandas as pd

        self._check(day)
        ts = pd.Timestamp(day)
        if not self._cal.is_session(ts):
            return None
        open_, close = self._cal.session_open_close(ts)
        return Session(day, open_.to_pydatetime(), close.to_pydatetime())

    def sessions(self, start: date, end: date) -> list[Session]:
        import pandas as pd

        start = max(start, self._first)
        end = min(end, self._last)
        if start > end:
            return []
        frame = self._cal.schedule.loc[pd.Timestamp(start) : pd.Timestamp(end)]
        return [
            Session(idx.date(), row.open.to_pydatetime(), row.close.to_pydatetime())
            for idx, row in frame.iterrows()
        ]

    def is_regular_weekday(self, day: date) -> bool:
        return self._weekmask[day.weekday()] == "1"


# ---- registry ------------------------------------------------------------------

CalendarFactory = Callable[[], MarketCalendar]

_FACTORIES: dict[str, CalendarFactory] = {"24/7": AlwaysOpenCalendar}
_INSTANCES: dict[str, MarketCalendar] = {}
_LOCK = threading.Lock()


def register_calendar(name: str, factory: CalendarFactory) -> None:
    """Register (or replace) the calendar behind ``name``."""
    with _LOCK:
        _FACTORIES[name] = factory
        _INSTANCES.pop(name, None)


def get_calendar(name: str) -> MarketCalendar:
    """The calendar registered as ``name``; any other name is looked up in
    ``exchange_calendars`` (ISO MIC codes such as ``XNYS``). Instances are
    cached: building an exchange calendar takes a moment."""
    with _LOCK:
        cached = _INSTANCES.get(name)
        if cached is not None:
            return cached
        factory = _FACTORIES.get(name)
        cal = factory() if factory is not None else ExchangeCalendar(name)
        _INSTANCES[name] = cal
        return cal


#: Our canonical exchange codes (ticker suffixes) -> calendar names.
EXCHANGE_CALENDARS: dict[str, str] = {
    "US": "XNYS",
    "NYSE": "XNYS",
    "NASDAQ": "XNYS",
    "LSE": "XLON",
    "XETRA": "XETR",
    "F": "XFRA",
    "PA": "XPAR",
    "AS": "XAMS",
    "BR": "XBRU",
    "LS": "XLIS",
    "MC": "XMAD",
    "MI": "XMIL",
    "SW": "XSWX",
    "VI": "XWBO",
    "CO": "XCSE",
    "ST": "XSTO",
    "OL": "XOSL",
    "HE": "XHEL",
    "IR": "XDUB",
    "TO": "XTSE",
    "V": "XTSE",
    "AU": "XASX",
    "HK": "XHKG",
    "SHG": "XSHG",
    "SHE": "XSHG",
    "KO": "XKRX",
    "KQ": "XKRX",
    "NSE": "XBOM",
    "BSE": "XBOM",
    "SA": "BVMF",
    "MX": "XMEX",
    "JSE": "XJSE",
    "TA": "XTAE",
    # Virtual exchanges for non-equity classes.
    "CC": "24/7",
    "COMM": "CMES",
    "GBOND": "24/5",
    "FOREX": "24/5",
}

#: Asset classes whose calendar doesn't depend on the listing exchange.
ASSET_CLASS_CALENDARS: dict[str, str] = {"crypto": "24/7"}


def calendar_for_exchange(code: str) -> MarketCalendar:
    name = EXCHANGE_CALENDARS.get(code.upper())
    if name is None:
        raise UnknownCalendarError(f"no calendar mapped for exchange {code!r}")
    return get_calendar(name)


def calendar_for_ticker(ticker: str, asset_class: str | None = None) -> MarketCalendar:
    """By asset class when it fixes the calendar (crypto), else by the
    ticker's exchange suffix (``AAPL.US`` -> NYSE)."""
    if asset_class in ASSET_CLASS_CALENDARS:
        return get_calendar(ASSET_CLASS_CALENDARS[asset_class])
    if "." not in ticker:
        raise UnknownCalendarError(f"{ticker!r} has no exchange suffix")
    return calendar_for_exchange(ticker.rsplit(".", 1)[1])


def trading_calendars(
    universe: Iterable[str], asset_classes: Mapping[str, str]
) -> tuple[list[MarketCalendar], list[str]]:
    """The distinct calendars a universe trades on, plus the tickers that
    couldn't be placed on any calendar."""
    by_name: dict[str, MarketCalendar] = {}
    unknown: list[str] = []
    for ticker in universe:
        try:
            cal = calendar_for_ticker(ticker, asset_classes.get(ticker))
        except UnknownCalendarError:
            unknown.append(ticker)
            continue
        by_name.setdefault(cal.name, cal)
    return list(by_name.values()), unknown


def universe_trades_on(
    universe: Sequence[str], day: date, asset_classes: Mapping[str, str]
) -> bool:
    """True when any instrument in ``universe`` has a session on ``day``.

    Conservative on purpose: an empty universe, a ticker with no known
    calendar, or a date outside a calendar's range all count as trading,
    so a calendar gap can never silently stop the book from trading.
    """
    if not universe:
        return True
    calendars, unknown = trading_calendars(universe, asset_classes)
    if unknown:
        return True
    for cal in calendars:
        try:
            if cal.is_trading_day(day):
                return True
        except CalendarRangeError:
            return True
    return False


class TickerSessionCalendar:
    """The ingest quality checker's calendar (``sessions(ticker, start,
    end)``, see ``stonks.ingest.quality``) over these market calendars:
    each ticker's session dates on its exchange's calendar, or ``None``
    (the checker then skips the gap rule) when the ticker has no known
    calendar or the range is outside it."""

    def sessions(self, ticker: str, start: date, end: date) -> list[date] | None:
        try:
            cal = calendar_for_ticker(ticker)
            return [s.date for s in cal.sessions(start, end)]
        except (UnknownCalendarError, CalendarRangeError):
            return None

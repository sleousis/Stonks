"""A point-in-time view of the lake (BL-49, principle P12).

``PointInTimeLake(lake, as_of)`` is a read-only proxy over a
:class:`~stonks.store.lake.DuckDBLake` that clamps every read to what was
known at a decision on the bar starting at ``as_of``. The backtest engine
and the tick hand it to strategies instead of the lake, so a strategy
cannot read the future even by mistake (a wrong ``as_of``, a missing
filter, a whole-history read).

What "known" means (``stonks.core.interval``, one rule for the whole
system): the decision is made at the close of its bar, ``as_of + L`` (the
*reach*, ``L`` = ``decision_interval``; without one a midnight ``as_of`` is
a daily decision). Then:

- **bars** are visible when complete: a bar of interval ``I`` stamped
  ``S`` when ``S + I <= reach`` (:meth:`PointInTimeLake.bar_cutoff`);
- **day-stamped rows** are known once their day has ended
  (:attr:`PointInTimeLake.known_through`): statements by
  ``available_date`` (the day after the filing date, since a filing may
  land after the close: usable from the decision bar's start day, daily
  or intraday), macro prints by publication date,
  share counts, dividends, splits, bond yields and TVL by their day;
- **universe membership** shows spans that started by then, and an exit
  dated later reads as still open (nobody knew it yet);
- **names** (``bar_tickers``) are listed once their first bar is visible;
- static profile reads (asset classes, sectors) pass through;
- raw ``sql()`` raises :class:`PointInTimeViolation` unless the view was
  built with ``allow_raw=True``, and nothing else of the lake (writes, the
  connection) is reachable. A reader the lake itself lacks is missing on
  the view too, so ``getattr(lake, name, None)`` checks keep working.

Views of one run share a :class:`PitSession`, which reads each full
history (bars of a series, a statement table, a macro series) from the
lake once and slices it per view in memory. A run makes one session and
one cheap view per decision: ``session.at(as_of)``.
"""

from __future__ import annotations

from collections.abc import Callable, Hashable, Mapping
from datetime import date, datetime, timedelta
from typing import Any

import pandas as pd

from stonks.core.interval import Interval, decision_reach, known_through, visible_cutoff
from stonks.core.timeutil import as_datetime

__all__ = ["PitSession", "PointInTimeLake", "PointInTimeViolation"]

# Bounds wide enough to cover every bar a lake can hold.
_HISTORY_START = datetime(1900, 1, 1)
_HISTORY_END = datetime(2200, 1, 1)


class PointInTimeViolation(PermissionError):
    """A read through a point-in-time view could see the future."""


def _day(value: Any) -> date:
    """The calendar day of a date, datetime or timestamp."""
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    return as_datetime(pd.Timestamp(value).to_pydatetime()).date()


def _on_or_before(column: pd.Series, day: date) -> pd.Series:
    """Rows whose day stamp in ``column`` is known by ``day`` (NULLs are not)."""
    stamps = pd.to_datetime(column, errors="coerce")
    return (stamps.notna() & (stamps <= pd.Timestamp(day))).astype(bool)


def _rows(frame: pd.DataFrame | None, mask: pd.Series) -> pd.DataFrame:
    if frame is None:
        return pd.DataFrame()
    return pd.DataFrame(frame.loc[mask.to_numpy()]).reset_index(drop=True)


class PitSession:
    """The full-history reads of one lake, shared by the views of one run.

    ``cached(key, load)`` loads once per key. Rows written to the lake after
    a key was first read are not seen, so scope a session to one run (one
    backtest, one tick)."""

    def __init__(self, lake: Any) -> None:
        self.lake = lake
        self._cache: dict[Hashable, Any] = {}

    def at(
        self,
        as_of: Any,
        *,
        decision_interval: Interval | None = None,
        allow_raw: bool = False,
    ) -> PointInTimeLake:
        """The view of this session's lake at a decision on ``as_of``."""
        return PointInTimeLake(
            self.lake,
            as_of,
            decision_interval=decision_interval,
            allow_raw=allow_raw,
            session=self,
        )

    def cached(self, key: Hashable, load: Callable[[], Any]) -> Any:
        if key not in self._cache:
            self._cache[key] = load()
        return self._cache[key]

    def __deepcopy__(self, memo: dict[int, Any]) -> PitSession:
        return self  # shared state, like the lake it reads


#: Reader name -> the lake method it needs (the view lacks it when the lake does).
_READERS: Mapping[str, str] = {
    "get_bars": "get_bars",
    "get_prices": "get_prices",
    "get_statement_history": "get_statement_history",
    "get_statements_as_of": "get_statements_as_of",
    "get_income_statement": "get_income_statement",
    "get_balance_sheet": "get_balance_sheet",
    "get_cash_flow_statement": "get_cash_flow_statement",
    "get_macro_series": "get_macro_series",
    "get_shares_outstanding": "get_shares_outstanding",
    "get_dividends": "get_dividends",
    "get_corporate_actions": "get_corporate_actions",
    "get_bond_yields": "get_bond_yields",
    "get_defi_tvl": "get_defi_tvl",
    "members_as_of": "members_between",
    "members_between": "members_between",
    "get_universe_membership": "get_universe_membership",
    "universe_ids": "universe_ids",
    "get_asset_classes": "get_asset_classes",
    "instrument_sectors": "instrument_sectors",
    "bar_tickers": "bar_first_stamps",
    "sql": "sql",
}


class PointInTimeLake:
    """A read-only view of ``lake`` at a decision on ``as_of`` (module doc)."""

    def __init__(
        self,
        lake: Any,
        as_of: Any,
        *,
        decision_interval: Interval | None = None,
        allow_raw: bool = False,
        session: PitSession | None = None,
    ) -> None:
        self._lake = lake
        self._as_of = as_datetime(as_of)
        self._decision = decision_interval
        self._allow_raw = allow_raw
        self._session = session if session is not None else PitSession(lake)
        self._reach = decision_reach(self._as_of, decision_interval)
        self._known = known_through(self._as_of, decision_interval)
        #: Filings carry no time of day, so one is used from the day after
        #: it (``available_date``): by the decision bar's start day, daily or
        #: intraday (BE-22).
        self._filed_by = self._as_of.date()

    # ---- the decision ---------------------------------------------------------

    @property
    def as_of(self) -> datetime:
        """The start of the decision bar."""
        return self._as_of

    @property
    def decision_interval(self) -> Interval | None:
        return self._decision

    @property
    def reach(self) -> datetime:
        """The close of the decision bar: nothing after it is known."""
        return self._reach

    @property
    def known_through(self) -> date:
        """The last day whose day-stamped rows are known."""
        return self._known

    @property
    def pit_session(self) -> PitSession:
        """The session this view reads through (shared by one run's views)."""
        return self._session

    def bar_cutoff(self, interval: Interval) -> datetime:
        """The latest visible stamp of an ``interval`` bar."""
        return visible_cutoff(self._as_of, interval, self._decision)

    def __repr__(self) -> str:
        return f"PointInTimeLake(as_of={self._as_of.isoformat()})"

    def __deepcopy__(self, memo: dict[int, Any]) -> PointInTimeLake:
        return self  # a view, like the lake it wraps, is shared, never copied

    # ---- dispatch ---------------------------------------------------------------

    def __getattr__(self, name: str) -> Any:
        needs = _READERS.get(name)
        if needs is None:
            raise AttributeError(f"{name!r} is not a point-in-time read of the lake")
        if not callable(getattr(self._lake, needs, None)):
            raise AttributeError(f"the lake has no {needs!r}")
        return getattr(self, f"_read_{name}")

    def _cached(self, key: Hashable, load: Callable[[], Any]) -> Any:
        return self._session.cached(key, load)

    def _clamp_day(self, value: Any) -> date:
        return self._known if value is None else min(_day(value), self._known)

    # ---- bars ------------------------------------------------------------------

    def _read_get_bars(self, ticker: str, interval: Interval, start: Any, end: Any) -> pd.DataFrame:
        full = self._cached(
            ("bars", ticker, interval.code),
            lambda: self._lake.get_bars(ticker, interval, _HISTORY_START, _HISTORY_END),
        )
        if full is None or full.empty:
            return pd.DataFrame(columns=getattr(full, "columns", None))
        stamps = pd.to_datetime(full["timestamp"])
        hi = self.bar_cutoff(interval)
        if end is not None:
            hi = min(hi, as_datetime(end))
        mask = stamps <= pd.Timestamp(hi)
        if start is not None:
            mask &= stamps >= pd.Timestamp(as_datetime(start))
        return _rows(full, mask)

    def _read_get_prices(self, ticker: str, start: Any, end: Any) -> pd.DataFrame:
        last = self.bar_cutoff(Interval.DAY_1).date()
        return self._lake.get_prices(ticker, start, last if end is None else min(_day(end), last))

    def _read_bar_tickers(self, interval: Interval) -> list[str]:
        first = self._cached(
            ("bar_first_stamps", interval.code), lambda: self._lake.bar_first_stamps(interval)
        )
        cutoff = self.bar_cutoff(interval)
        return sorted(t for t, ts in first.items() if ts <= cutoff)

    # ---- statements --------------------------------------------------------------

    def _read_get_statement_history(
        self, statement: str, ticker: str, *, missing_filing_lag_days: int = 90
    ) -> pd.DataFrame:
        full = self._cached(
            ("statement_history", statement, ticker, missing_filing_lag_days),
            lambda: self._lake.get_statement_history(
                statement, ticker, missing_filing_lag_days=missing_filing_lag_days
            ),
        )
        return _rows(full, _on_or_before(full["available_date"], self._filed_by))

    def _read_get_statements_as_of(
        self, statement: str, ticker: str, as_of: Any, **kwargs: Any
    ) -> pd.DataFrame:
        day = self._filed_by if as_of is None else min(_day(as_of), self._filed_by)
        return self._lake.get_statements_as_of(statement, ticker, day, **kwargs)

    def _statement(self, table: str, ticker: str) -> pd.DataFrame:
        """Rows filed before the decision day: a filing is used from the
        day after it (BE-22)."""
        full = self._cached((table, ticker), lambda: getattr(self._lake, f"get_{table}")(ticker))
        return _rows(full, _on_or_before(full["filing_date"], self._filed_by - timedelta(days=1)))

    def _read_get_income_statement(self, ticker: str) -> pd.DataFrame:
        return self._statement("income_statement", ticker)

    def _read_get_balance_sheet(self, ticker: str) -> pd.DataFrame:
        return self._statement("balance_sheet", ticker)

    def _read_get_cash_flow_statement(self, ticker: str) -> pd.DataFrame:
        return self._statement("cash_flow_statement", ticker)

    # ---- macro and dated metadata ------------------------------------------------------

    def _read_get_macro_series(
        self,
        country_iso: str,
        indicator: str,
        *,
        as_of: Any = None,
        publication_lag_days: int = 0,
        stamped_at: str = "period_start",
    ) -> pd.DataFrame:
        full = self._cached(
            ("macro", country_iso, indicator, publication_lag_days, stamped_at),
            lambda: self._lake.get_macro_series(
                country_iso,
                indicator,
                publication_lag_days=publication_lag_days,
                stamped_at=stamped_at,
            ),
        )
        return _rows(full, _on_or_before(full["available_date"], self._clamp_day(as_of)))

    def _dated(self, key: Hashable, load: Callable[[], pd.DataFrame], column: str) -> pd.DataFrame:
        full = self._cached(key, load)
        return _rows(full, _on_or_before(full[column], self._known))

    def _read_get_shares_outstanding(self, ticker: str) -> pd.DataFrame:
        return self._dated(
            ("shares", ticker), lambda: self._lake.get_shares_outstanding(ticker), "date"
        )

    def _read_get_dividends(self, ticker: str) -> pd.DataFrame:
        return self._dated(
            ("dividends", ticker), lambda: self._lake.get_dividends(ticker), "ex_date"
        )

    def _read_get_corporate_actions(self, tickers: list[str]) -> pd.DataFrame:
        key = ("corporate_actions", tuple(tickers))
        return self._dated(key, lambda: self._lake.get_corporate_actions(list(tickers)), "ex_date")

    def _read_get_bond_yields(self, ticker: str, *, as_of: Any = None) -> pd.DataFrame:
        full = self._cached(("bond_yields", ticker), lambda: self._lake.get_bond_yields(ticker))
        return _rows(full, _on_or_before(full["date"], self._clamp_day(as_of)))

    def _read_get_defi_tvl(self, chain: str, *, start: Any = None, end: Any = None) -> pd.DataFrame:
        full = self._cached(("defi_tvl", chain), lambda: self._lake.get_defi_tvl(chain))
        mask = _on_or_before(full["observation_date"], self._clamp_day(end))
        if start is not None:
            stamps = pd.to_datetime(full["observation_date"], errors="coerce")
            mask &= (stamps >= pd.Timestamp(_day(start))).astype(bool)
        return _rows(full, mask)

    # ---- universe membership ---------------------------------------------------------

    def _read_members_as_of(self, universe_id: str, as_of: Any) -> list[str]:
        return self._read_members_between(universe_id, as_of, as_of)

    def _read_members_between(self, universe_id: str, start: Any, end: Any) -> list[str]:
        # a window reaching past the decision reads as the decision day
        return self._lake.members_between(universe_id, self._clamp_day(start), self._clamp_day(end))

    def _read_get_universe_membership(
        self, universe_id: str | None = None, tickers: list[str] | None = None
    ) -> pd.DataFrame:
        frame: pd.DataFrame | None = self._lake.get_universe_membership(universe_id, tickers)
        if frame is None or frame.empty:
            return pd.DataFrame() if frame is None else frame
        out = _rows(frame, _on_or_before(pd.Series(frame["start_date"]), self._known))
        # an exit after the decision was not known yet: the span reads open
        unknown = ~_on_or_before(pd.Series(out["end_date"]), self._known)
        out["end_date"] = out["end_date"].astype(object).where(~unknown, None)
        return out

    def _read_universe_ids(self) -> list[str]:
        return self._lake.universe_ids()

    # ---- static profile data --------------------------------------------------------

    def _read_get_asset_classes(self, tickers: list[str]) -> dict[str, str]:
        return self._lake.get_asset_classes(tickers)

    def _read_instrument_sectors(self, tickers: list[str]) -> pd.DataFrame:
        return self._lake.instrument_sectors(tickers)

    # ---- raw SQL -------------------------------------------------------------------

    def _read_sql(self, query: str, params: list[Any] | None = None) -> pd.DataFrame:
        if not self._allow_raw:
            raise PointInTimeViolation(
                "raw sql() through a point-in-time lake can read the future; use a "
                "typed read (get_bars, get_statement_history, ...) or build the view "
                "with allow_raw=True"
            )
        return self._lake.sql(query, params)

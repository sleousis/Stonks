"""Lake persistence for the event calendars (migration 020) and the news
reads the console shows (migration 002 tables ``news``, ``news_sentiment``).

Writes are idempotent upserts keyed as in the migration. Reads take a date
window and an optional ticker list; an empty list reads nothing (a scope
with no tickers has no events), ``None`` reads everything.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from datetime import UTC, date, datetime, timedelta
from typing import TYPE_CHECKING, Any

import pandas as pd

from stonks.calendars.importance import ImportanceRater, default_rater
from stonks.calendars.models import (
    DividendEvent,
    EarningsEvent,
    EconomicEvent,
    NewsItem,
    SentimentDay,
)
from stonks.ingest.calendar_schemas import DividendEventRow, EarningsEventRow, EconomicEventRow

if TYPE_CHECKING:  # pragma: no cover
    from stonks.store.lake import DuckDBLake

_EARNINGS_COLS = (
    "ticker",
    "period_end",
    "report_date",
    "before_after_market",
    "currency",
    "eps_estimate",
    "eps_actual",
    "eps_difference",
    "surprise_percent",
    "source",
    "updated_at",
)
_DIVIDEND_COLS = (
    "ticker",
    "ex_date",
    "amount",
    "currency",
    "record_date",
    "pay_date",
    "declaration_date",
    "source",
    "updated_at",
)
_ECONOMIC_COLS = (
    "country",
    "event_time",
    "event_type",
    "comparison",
    "period",
    "actual",
    "previous",
    "estimate",
    "change",
    "change_pct",
    "source",
    "updated_at",
)


def _now() -> datetime:
    return datetime.now(UTC).replace(tzinfo=None)


def _frame(rows: Iterable[Any], source: str, cols: tuple[str, ...]) -> pd.DataFrame:
    stamp = _now()
    records = [{**row.model_dump(), "source": source, "updated_at": stamp} for row in rows]
    return pd.DataFrame(records, columns=list(cols))


def _clean(value: Any) -> Any:
    """``None`` for pandas NA/NaN/NaT, python dates and datetimes otherwise."""
    if value is None:
        return None
    if isinstance(value, list | tuple):
        return list(value)
    if hasattr(value, "tolist") and not isinstance(value, str):
        # numpy arrays (VARCHAR[] columns)
        try:
            return list(value.tolist())
        except TypeError:
            pass
    if pd.isna(value):
        return None
    if isinstance(value, pd.Timestamp):
        return value.to_pydatetime()
    return value


def _records(df: pd.DataFrame) -> list[dict[str, Any]]:
    return [{k: _clean(v) for k, v in r.items()} for r in df.to_dict(orient="records")]


def _as_date(value: Any) -> date:
    return value.date() if isinstance(value, datetime) else value


def _ticker_clause(tickers: Sequence[str] | None, column: str) -> tuple[str, list[Any]]:
    if tickers is None:
        return "", []
    return f" AND {column} = ANY(?)", [list(tickers)]


class CalendarStore:
    """Reads and writes the calendars on an open lake."""

    def __init__(self, lake: DuckDBLake, *, rater: ImportanceRater | None = None) -> None:
        self._lake = lake
        self._rater = rater or default_rater()

    # ---- writes ----------------------------------------------------------------

    def upsert_earnings(self, rows: Iterable[EarningsEventRow], *, source: str) -> int:
        """Last write wins per ``(ticker, period_end)``: a moved report
        date replaces the old one."""
        df = _frame(rows, source, _EARNINGS_COLS)
        return self._lake._upsert(
            df, table="earnings_calendar", cols=_EARNINGS_COLS, pk=("ticker", "period_end")
        )

    def upsert_dividends(self, rows: Iterable[DividendEventRow], *, source: str) -> int:
        """Per ``(ticker, ex_date)``. A NULL never overwrites a stored value:
        one vendor lists only the date, another the amount."""
        df = _frame(rows, source, _DIVIDEND_COLS)
        return self._lake._upsert_preserve_nulls(
            df, table="dividend_calendar", cols=_DIVIDEND_COLS, pk=("ticker", "ex_date")
        )

    def upsert_economic(self, rows: Iterable[EconomicEventRow], *, source: str) -> int:
        df = _frame(rows, source, _ECONOMIC_COLS)
        if not df.empty:
            # naive UTC in the lake, like every other timestamp
            df["event_time"] = pd.to_datetime(df["event_time"], utc=True).dt.tz_localize(None)
        return self._lake._upsert(
            df,
            table="economic_events",
            cols=_ECONOMIC_COLS,
            pk=("country", "event_time", "event_type", "comparison"),
        )

    # ---- reads -----------------------------------------------------------------

    def earnings(
        self, start: date, end: date, *, tickers: Sequence[str] | None = None
    ) -> list[EarningsEvent]:
        """Reports with ``start <= report_date <= end``, by date then ticker."""
        if tickers is not None and not tickers:
            return []
        clause, params = _ticker_clause(tickers, "e.ticker")
        df = self._lake.sql(
            f"""
            SELECT e.ticker, i.name, e.period_end, e.report_date, e.before_after_market,
                   e.currency, e.eps_estimate, e.eps_actual, e.eps_difference,
                   e.surprise_percent
              FROM earnings_calendar e LEFT JOIN instruments i ON i.id = e.ticker
             WHERE e.report_date BETWEEN ? AND ?{clause}
             ORDER BY e.report_date, e.ticker
            """,
            [start, end, *params],
        )
        out = []
        for r in _records(df):
            r["period_end"] = _as_date(r["period_end"])
            r["report_date"] = _as_date(r["report_date"])
            out.append(EarningsEvent(**r))
        return out

    def dividends(
        self, start: date, end: date, *, tickers: Sequence[str] | None = None
    ) -> list[DividendEvent]:
        """Ex-dividend dates in the window. A calendar row without an amount
        takes it (and the other dates) from ``dividends`` when that table
        has the same ex-date."""
        if tickers is not None and not tickers:
            return []
        clause, params = _ticker_clause(tickers, "c.ticker")
        df = self._lake.sql(
            f"""
            SELECT c.ticker, i.name, c.ex_date,
                   COALESCE(c.amount, d.amount) AS amount,
                   COALESCE(c.currency, d.currency) AS currency,
                   COALESCE(c.record_date, d.record_date) AS record_date,
                   COALESCE(c.pay_date, d.pay_date) AS pay_date,
                   COALESCE(c.declaration_date, d.declaration_date) AS declaration_date
              FROM dividend_calendar c
              LEFT JOIN dividends d ON d.ticker = c.ticker AND d.ex_date = c.ex_date
              LEFT JOIN instruments i ON i.id = c.ticker
             WHERE c.ex_date BETWEEN ? AND ?{clause}
             ORDER BY c.ex_date, c.ticker
            """,
            [start, end, *params],
        )
        out = []
        for r in _records(df):
            for key in ("ex_date", "record_date", "pay_date", "declaration_date"):
                if r[key] is not None:
                    r[key] = _as_date(r[key])
            out.append(DividendEvent(**r))
        return out

    def economic(
        self, start: date, end: date, *, countries: Sequence[str] | None = None
    ) -> list[EconomicEvent]:
        """Releases on the UTC days ``start`` to ``end``, in time order."""
        clause = ""
        params: list[Any] = [
            datetime.combine(start, datetime.min.time()),
            datetime.combine(end + timedelta(days=1), datetime.min.time()),
        ]
        if countries is not None:
            clause = " AND country = ANY(?)"
            params.append([c.upper() for c in countries])
        df = self._lake.sql(
            f"""
            SELECT country, event_time, event_type, comparison, period, actual, previous,
                   estimate, change, change_pct
              FROM economic_events
             WHERE event_time >= ? AND event_time < ?{clause}
             ORDER BY event_time, country, event_type, comparison
            """,
            params,
        )
        out = []
        for r in _records(df):
            r["event_time"] = r["event_time"].replace(tzinfo=UTC)
            out.append(EconomicEvent(**r, importance=self._rater.rate(r["event_type"])))
        return out

    def news(
        self, tickers: Sequence[str], *, limit: int, since: date | None = None
    ) -> list[NewsItem]:
        """The newest articles on ``tickers`` first."""
        if not tickers:
            return []
        params: list[Any] = [list(tickers)]
        clause = ""
        if since is not None:
            clause = " AND published_at >= ?"
            params.append(datetime.combine(since, datetime.min.time()))
        df = self._lake.sql(
            f"""
            SELECT ticker, published_at, title, url, source_name, sentiment, tags
              FROM news
             WHERE ticker = ANY(?){clause}
             ORDER BY published_at DESC, ticker, title
             LIMIT ?
            """,
            [*params, limit],
        )
        out = []
        for r in _records(df):
            r["tags"] = r["tags"] or []
            r["published_at"] = r["published_at"].replace(tzinfo=UTC)
            out.append(NewsItem(**r))
        return out

    def sentiment(self, tickers: Sequence[str], *, since: date) -> list[SentimentDay]:
        """Daily sentiment of ``tickers`` from ``since``, oldest first."""
        if not tickers:
            return []
        df = self._lake.sql(
            """
            SELECT ticker, date AS day, sentiment, article_count
              FROM news_sentiment
             WHERE ticker = ANY(?) AND date >= ?
             ORDER BY date, ticker
            """,
            [list(tickers), since],
        )
        out = []
        for r in _records(df):
            r["day"] = _as_date(r["day"])
            out.append(SentimentDay(**r))
        return out

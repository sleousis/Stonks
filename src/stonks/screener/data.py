"""What a screen reads from the lake, loaded once per screen and date.

Everything is point in time (P12): bars up to the date, statements whose
filing date is on or before it (a missing filing date counts as the period
end plus :data:`FILING_LAG_DAYS`), dividends with an ex date on or before
it. A ticker whose last daily bar is more than :data:`STALE_DAYS` old has
no price on the date, so it has no price metric either.

The built-in metrics read set-based aggregates (:attr:`ScreenData.price_stats`
and :attr:`ScreenData.income`): one DuckDB query for all tickers, never a
Python loop per ticker (roadmap 20.11). :attr:`ScreenData.bars` and
:attr:`ScreenData.adjusted` stay for metrics that need the raw series. They
load only when such a metric asks.
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from datetime import date, datetime, timedelta
from functools import cached_property
from typing import TYPE_CHECKING, Any

import numpy as np
import pandas as pd

from stonks.core.interval import Interval

if TYPE_CHECKING:  # pragma: no cover
    from stonks.store.lake import DuckDBLake

#: Calendar days of bars loaded before the date (a year of sessions plus room).
BAR_LOOKBACK_DAYS = 400
#: A last bar older than this many days means no price on the date.
STALE_DAYS = 10
#: Days after the period end a statement counts as known when the vendor
#: gave no filing date.
FILING_LAG_DAYS = 45
#: A stored market cap older than this many days is not used.
MARKET_CAP_MAX_AGE_DAYS = 10
#: Sessions in a month, a quarter, half a year and a year.
MONTH, QUARTER, HALF, YEAR = 21, 63, 126, 252
#: The trailing-return lags :attr:`ScreenData.price_stats` carries.
PRICE_LAGS = (MONTH, QUARTER, HALF, YEAR)
#: Sessions in the dollar volume average.
DOLLAR_VOLUME_SESSIONS = 20
#: Fewest prices a volatility needs.
MIN_VOL_PRICES = 21
#: Most days four quarters of a trailing sum may span, end to end.
TTM_MAX_SPAN_DAYS = 300

#: When a statement row counts as known on the date (P12).
_KNOWN = f"COALESCE(filing_date, CAST(period_end + INTERVAL {FILING_LAG_DAYS} DAY AS DATE))"
_BALANCE_COLS = (
    "total_stockholder_equity",
    "short_long_term_debt_total",
    "long_term_debt",
    "short_term_debt",
    "common_stock_shares_outstanding",
)


def finite(values: dict[str, Any]) -> dict[str, float]:
    """``values`` without missing, infinite or non-numeric entries."""
    out: dict[str, float] = {}
    for ticker, value in values.items():
        try:
            number = float(value)
        except (TypeError, ValueError):
            continue
        if math.isfinite(number):
            out[ticker] = number
    return out


def _nan_null(col: str) -> str:
    """``col`` with NaN read as missing, as pandas does."""
    return f"CASE WHEN isnan({col}) THEN NULL ELSE {col} END"


class ScreenData:
    """Lazy, cached inputs of one screen on one date."""

    def __init__(self, lake: DuckDBLake, tickers: Sequence[str], as_of: date) -> None:
        self.lake = lake
        self.tickers = list(dict.fromkeys(tickers))
        self.as_of = as_of
        self._values: dict[str, dict[str, float]] = {}

    def metric(self, metric_id: str) -> dict[str, float]:
        """The values of one registered metric (cached)."""
        if metric_id not in self._values:
            from stonks.screener.registry import metric_for

            self._values[metric_id] = finite(metric_for(metric_id).compute(self))
        return self._values[metric_id]

    # ---- bars ------------------------------------------------------------------------

    def _bar_window(self) -> list[Any]:
        """Parameters that pick the fresh daily bars up to the date."""
        start = datetime.combine(
            self.as_of - timedelta(days=BAR_LOOKBACK_DAYS), datetime.min.time()
        )
        end = datetime.combine(self.as_of + timedelta(days=1), datetime.min.time())
        fresh = datetime.combine(self.as_of - timedelta(days=STALE_DAYS), datetime.min.time())
        return [str(Interval.DAY_1), self.tickers, start, end, fresh]

    @cached_property
    def bars(self) -> pd.DataFrame:
        """Daily bars (``ticker, day, close, adj_close, volume``) of the
        tickers that still traded near the date, oldest first. The built-in
        metrics never load it (see :attr:`price_stats`)."""
        cols = ["ticker", "day", "close", "adj_close", "volume"]
        if not self.tickers:
            return pd.DataFrame(columns=cols)
        df = self.lake.con.execute(
            """SELECT ticker, CAST(timestamp AS DATE) AS day, close, adj_close, volume
                 FROM bars
                WHERE interval = ? AND ticker = ANY(?) AND timestamp >= ? AND timestamp < ?
              QUALIFY max(timestamp) OVER (PARTITION BY ticker) >= ?
                ORDER BY ticker, timestamp""",
            self._bar_window(),
        ).df()
        return df if not df.empty else pd.DataFrame(columns=cols)

    @cached_property
    def adjusted(self) -> dict[str, np.ndarray]:
        """Adjusted closes per ticker, oldest first (the close where the
        vendor gave no adjusted one)."""
        df = self.bars
        if df.empty:
            return {}
        price = df["adj_close"].where(df["adj_close"].notna(), df["close"])
        frame = df.assign(price=price).dropna(subset=["price"])
        return {str(t): g["price"].to_numpy(dtype=float) for t, g in frame.groupby("ticker")}

    @cached_property
    def price_stats(self) -> pd.DataFrame:
        """Price aggregates of every fresh ticker from one query, indexed by
        ticker: ``last_close`` (the last close), ``dollar_volume`` (mean
        close times volume of the last :data:`DOLLAR_VOLUME_SESSIONS`
        sessions), ``price`` (the last adjusted close, the close where the
        vendor gave none), ``price_<n>`` (the adjusted close ``n`` sessions
        before, for each of :data:`PRICE_LAGS`), ``high_252``,
        ``volatility`` (annualised, over :data:`QUARTER` sessions), and the
        counts the metrics check (``n_prices``, ``n_vol``, ``low_vol``)."""
        if not self.tickers:
            return pd.DataFrame()
        lags = ", ".join(f"max(price) FILTER (WHERE rn = {n + 1}) AS price_{n}" for n in PRICE_LAGS)
        df = self.lake.con.execute(
            f"""WITH b AS (
                    SELECT ticker, timestamp, {_nan_null("close")} AS close,
                           {_nan_null("adj_close")} AS adj_close, volume
                      FROM bars
                     WHERE interval = ? AND ticker = ANY(?) AND timestamp >= ? AND timestamp < ?
                   QUALIFY max(timestamp) OVER (PARTITION BY ticker) >= ?
                ),
                p AS (
                    SELECT ticker, price,
                           row_number() OVER w AS rn,
                           CASE WHEN price > 0 THEN ln(price) END
                             - CASE WHEN lead(price) OVER w > 0 THEN ln(lead(price) OVER w) END
                             AS log_return
                      FROM (SELECT ticker, timestamp, COALESCE(adj_close, close) AS price
                              FROM b WHERE COALESCE(adj_close, close) IS NOT NULL)
                    WINDOW w AS (PARTITION BY ticker ORDER BY timestamp DESC)
                ),
                prices AS (
                    SELECT ticker,
                           count(*) AS n_prices,
                           max(price) FILTER (WHERE rn = 1) AS price,
                           {lags},
                           max(price) FILTER (WHERE rn <= {YEAR}) AS high_252,
                           count(*) FILTER (WHERE rn <= {QUARTER + 1}) AS n_vol,
                           min(price) FILTER (WHERE rn <= {QUARTER + 1}) AS low_vol,
                           stddev_samp(log_return) FILTER (WHERE rn <= {QUARTER})
                             * sqrt({YEAR}) AS volatility
                      FROM p GROUP BY ticker
                ),
                closes AS (
                    SELECT ticker, arg_max(close, timestamp) AS last_close
                      FROM b WHERE close IS NOT NULL GROUP BY ticker
                ),
                traded AS (
                    SELECT ticker, avg(close * volume) AS dollar_volume
                      FROM (SELECT ticker, close, volume,
                                   row_number() OVER (
                                       PARTITION BY ticker ORDER BY timestamp DESC) AS rn
                              FROM b WHERE close IS NOT NULL AND volume IS NOT NULL)
                     WHERE rn <= {DOLLAR_VOLUME_SESSIONS}
                     GROUP BY ticker
                )
                SELECT COALESCE(p.ticker, c.ticker, t.ticker) AS ticker,
                       c.last_close, t.dollar_volume, p.* EXCLUDE (ticker)
                  FROM prices p
                  FULL JOIN closes c ON c.ticker = p.ticker
                  FULL JOIN traded t ON t.ticker = COALESCE(p.ticker, c.ticker)""",
            self._bar_window(),
        ).df()
        return df.set_index("ticker") if not df.empty else pd.DataFrame()

    def trailing_return(self, sessions: int) -> dict[str, float]:
        """The adjusted return over the last ``sessions`` sessions (one of
        :data:`PRICE_LAGS`), for tickers with that much history."""
        stats = self.price_stats
        if stats.empty:
            return {}
        then = stats[f"price_{sessions}"]
        ok = (stats["n_prices"] > sessions) & (then > 0)
        return finite((stats["price"][ok] / then[ok] - 1.0).to_dict())

    @property
    def volatility(self) -> dict[str, float]:
        """Annualised volatility of daily log returns over :data:`QUARTER`
        sessions, when there are enough prices and all are positive."""
        stats = self.price_stats
        if stats.empty:
            return {}
        ok = (stats["n_vol"] >= MIN_VOL_PRICES) & (stats["low_vol"] > 0)
        return finite(stats["volatility"][ok].to_dict())

    @property
    def from_high(self) -> dict[str, float]:
        """The last adjusted close against the highest of the last year."""
        stats = self.price_stats
        if stats.empty:
            return {}
        ok = stats["high_252"] > 0
        return finite((stats["price"][ok] / stats["high_252"][ok] - 1.0).to_dict())

    @property
    def dollar_volume(self) -> dict[str, float]:
        return self.column(self.price_stats, "dollar_volume")

    @property
    def last_close(self) -> dict[str, float]:
        return self.column(self.price_stats, "last_close")

    # ---- statements --------------------------------------------------------------------

    def _statement(self, table: str, cols: Sequence[str]) -> pd.DataFrame:
        return self.lake.con.execute(
            f"""SELECT ticker, period_end, frequency, {", ".join(cols)}
                  FROM {table}
                 WHERE ticker = ANY(?) AND {_KNOWN} <= ?
                 ORDER BY ticker, period_end DESC""",
            [self.tickers, self.as_of],
        ).df()

    @cached_property
    def income(self) -> pd.DataFrame:
        """Trailing-twelve-month ``revenue`` and ``net_income`` per ticker
        and the twelve months before (``revenue_prior``), from one query.
        Four quarters when the lake has them (all values present, spanning
        at most :data:`TTM_MAX_SPAN_DAYS`), else the latest annual
        statement and the one before it."""
        if not self.tickers:
            return pd.DataFrame()

        def four(col: str, first: int) -> str:
            rows = f"rn BETWEEN {first} AND {first + 3}"
            return (
                f"CASE WHEN count({col}) FILTER (WHERE {rows}) = 4"
                f" THEN sum({col}) FILTER (WHERE {rows}) END"
            )

        def whole(first: int) -> str:
            return (
                f"(count(*) FILTER (WHERE rn BETWEEN {first} AND {first + 3}) = 4"
                f" AND date_diff('day', max(period_end) FILTER (WHERE rn = {first + 3}),"
                f" max(period_end) FILTER (WHERE rn = {first})) <= {TTM_MAX_SPAN_DAYS})"
            )

        df = self.lake.con.execute(
            f"""WITH s AS (
                    SELECT ticker, period_end, frequency,
                           {_nan_null("revenue")} AS revenue,
                           {_nan_null("net_income")} AS net_income,
                           row_number() OVER (
                               PARTITION BY ticker, frequency ORDER BY period_end DESC) AS rn
                      FROM income_statement
                     WHERE ticker = ANY(?) AND {_KNOWN} <= ?
                ),
                q AS (
                    SELECT ticker, {whole(1)} AS now_ok, {whole(5)} AS prior_ok,
                           {four("revenue", 1)} AS revenue,
                           {four("net_income", 1)} AS net_income,
                           {four("revenue", 5)} AS revenue_prior
                      FROM s WHERE frequency = 'Q' GROUP BY ticker
                ),
                a AS (
                    SELECT ticker,
                           max(revenue) FILTER (WHERE rn = 1) AS revenue,
                           max(net_income) FILTER (WHERE rn = 1) AS net_income,
                           max(revenue) FILTER (WHERE rn = 2) AS revenue_prior
                      FROM s WHERE frequency = 'A' GROUP BY ticker
                )
                SELECT COALESCE(q.ticker, a.ticker) AS ticker,
                       CASE WHEN q.now_ok THEN q.revenue ELSE a.revenue END AS revenue,
                       CASE WHEN q.now_ok THEN q.net_income ELSE a.net_income END AS net_income,
                       CASE WHEN q.now_ok THEN (CASE WHEN q.prior_ok THEN q.revenue_prior END)
                            ELSE a.revenue_prior END AS revenue_prior
                  FROM q FULL JOIN a ON a.ticker = q.ticker
                 WHERE COALESCE(q.now_ok, FALSE) OR a.ticker IS NOT NULL""",
            [self.tickers, self.as_of],
        ).df()
        return df.set_index("ticker") if not df.empty else pd.DataFrame()

    @cached_property
    def balance(self) -> pd.DataFrame:
        """The latest balance sheet known on the date, per ticker, with
        ``total_debt``."""
        if not self.tickers:
            return pd.DataFrame()
        df = self._statement("balance_sheet", _BALANCE_COLS)
        latest = df.groupby("ticker").head(1).set_index("ticker")
        split = latest["long_term_debt"].fillna(0) + latest["short_term_debt"].fillna(0)
        has_split = latest["long_term_debt"].notna() | latest["short_term_debt"].notna()
        latest["total_debt"] = latest["short_long_term_debt_total"].where(
            latest["short_long_term_debt_total"].notna(), split.where(has_split)
        )
        return latest

    def column(self, frame: pd.DataFrame, name: str) -> dict[str, float]:
        if frame.empty or name not in frame:
            return {}
        return finite(frame[name].to_dict())

    # ---- market cap and dividends --------------------------------------------------------

    @cached_property
    def market_cap(self) -> dict[str, float]:
        """The stored market cap of the last :data:`MARKET_CAP_MAX_AGE_DAYS`
        days, else the last close times the shares outstanding."""
        out: dict[str, float] = {}
        if self.tickers:
            rows = self.lake.con.execute(
                """SELECT ticker, arg_max(market_cap, date) FROM market_cap_history
                    WHERE ticker = ANY(?) AND date <= ? AND date >= ?
                    GROUP BY ticker""",
                [
                    self.tickers,
                    self.as_of,
                    self.as_of - timedelta(days=MARKET_CAP_MAX_AGE_DAYS),
                ],
            ).fetchall()
            out = finite(dict(rows))
        shares = self.column(self.balance, "common_stock_shares_outstanding")
        for ticker, price in self.last_close.items():
            if ticker not in out and ticker in shares:
                out[ticker] = price * shares[ticker]
        return out

    @cached_property
    def dividends_ttm(self) -> dict[str, float]:
        """Cash dividends per share with an ex date in the last year."""
        if not self.tickers:
            return {}
        rows = self.lake.con.execute(
            """SELECT ticker, SUM(amount) FROM dividends
                WHERE ticker = ANY(?) AND ex_date <= ? AND ex_date > ?
                GROUP BY ticker""",
            [self.tickers, self.as_of, self.as_of - timedelta(days=365)],
        ).fetchall()
        return finite(dict(rows))

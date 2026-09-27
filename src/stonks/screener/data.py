"""What a screen reads from the lake, loaded once per screen and date.

Everything is point in time (P12): bars up to the date, the statement
version known on it, usable from the day after its filing date (a missing
filing date counts as the period end plus :data:`FILING_LAG_DAYS`), dividends with an ex date on or before
it. A ticker whose last daily bar is more than :data:`STALE_DAYS` old has
no price on the date, so it has no price metric either.
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from datetime import date, datetime, timedelta
from functools import cached_property
from typing import TYPE_CHECKING, Any, cast

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
#: gave no filing date (the lake's point-in-time default).
FILING_LAG_DAYS = 90
#: A stored market cap older than this many days is not used.
MARKET_CAP_MAX_AGE_DAYS = 10

_INCOME_COLS = ("revenue", "net_income")
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

    @cached_property
    def bars(self) -> pd.DataFrame:
        """Daily bars (``ticker, day, close, adj_close, volume``) of the
        tickers that still traded near the date, oldest first."""
        cols = ["ticker", "day", "close", "adj_close", "volume"]
        if not self.tickers:
            return pd.DataFrame(columns=cols)
        start = datetime.combine(
            self.as_of - timedelta(days=BAR_LOOKBACK_DAYS), datetime.min.time()
        )
        end = datetime.combine(self.as_of + timedelta(days=1), datetime.min.time())
        fresh = datetime.combine(self.as_of - timedelta(days=STALE_DAYS), datetime.min.time())
        df = self.lake.con.execute(
            """SELECT ticker, CAST(timestamp AS DATE) AS day, close, adj_close, volume
                 FROM bars
                WHERE interval = ? AND ticker = ANY(?) AND timestamp >= ? AND timestamp < ?
              QUALIFY max(timestamp) OVER (PARTITION BY ticker) >= ?
                ORDER BY ticker, timestamp""",
            [str(Interval.DAY_1), self.tickers, start, end, fresh],
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
    def last_close(self) -> dict[str, float]:
        df = self.bars.dropna(subset=["close"])
        return finite(df.groupby("ticker")["close"].last().to_dict()) if not df.empty else {}

    # ---- statements --------------------------------------------------------------------

    def _statement(self, table: str, cols: Sequence[str]) -> pd.DataFrame:
        """Per period, the version known on the date (P12): a version counts
        from the day after its filing (``period_end`` plus
        :data:`FILING_LAG_DAYS` when the vendor gave none), and a
        restatement Stonks saw after the date is not used. The same rule as
        ``DuckDBLake.get_statements_as_of``, for many tickers at once."""
        from stonks.core.timeutil import day_start
        from stonks.store.statement_versions import known_versions

        picked = ", ".join(cols)
        current = ", ".join(f"c.{c}" for c in cols)
        versions = f"{table}_versions"
        df = self.lake.con.execute(
            f"""WITH v AS (
                    SELECT ticker, period_end, frequency, filing_date, known_at, {picked}
                      FROM {versions} WHERE ticker = ANY(?)
                    UNION ALL BY NAME
                    SELECT c.ticker, c.period_end, c.frequency, c.filing_date,
                           CAST(COALESCE(c.filing_date, c.period_end) AS TIMESTAMP) AS known_at,
                           {current}
                      FROM {table} c
                     WHERE c.ticker = ANY(?)
                       AND NOT EXISTS (SELECT 1 FROM {versions} x
                                        WHERE x.ticker = c.ticker
                                          AND x.period_end = c.period_end
                                          AND x.frequency = c.frequency)
                )
                SELECT *,
                       CAST(GREATEST(COALESCE(filing_date + INTERVAL 1 DAY,
                                              period_end + to_days(CAST(? AS INTEGER))),
                                     period_end) AS DATE) AS available_date
                  FROM v
                 ORDER BY ticker, period_end, frequency, known_at""",
            [self.tickers, self.tickers, FILING_LAG_DAYS],
        ).df()
        columns = ["ticker", "period_end", "frequency", *cols]
        if df.empty:
            return pd.DataFrame(columns=columns)
        filed = pd.to_datetime(df["available_date"]) <= pd.Timestamp(self.as_of)
        known = known_versions(df, day_start(self.as_of + timedelta(days=1)), filed)
        out = known[columns]
        return out.sort_values(["ticker", "period_end"], ascending=[True, False]).reset_index(
            drop=True
        )

    @cached_property
    def income(self) -> pd.DataFrame:
        """Trailing-twelve-month ``revenue`` and ``net_income`` per ticker
        and the twelve months before (``revenue_prior``). Four quarters
        when the lake has them, else the latest annual statement."""
        out: dict[str, dict[str, float]] = {}
        if not self.tickers:
            return pd.DataFrame()
        df = self._statement("income_statement", _INCOME_COLS)
        for ticker, rows in df.groupby("ticker"):
            q = cast(pd.DataFrame, rows.loc[rows["frequency"] == "Q"])
            a = cast(pd.DataFrame, rows.loc[rows["frequency"] == "A"])
            now, prior = _trailing(q, 0), _trailing(q, 4)
            if now is None and not a.empty:
                now = a.iloc[0][list(_INCOME_COLS)].astype(float)
                prior = a.iloc[1][list(_INCOME_COLS)].astype(float) if len(a) > 1 else None
            if now is None:
                continue
            row = {c: _num(now, c) for c in _INCOME_COLS}
            row["revenue_prior"] = _num(prior, "revenue") if prior is not None else math.nan
            out[str(ticker)] = row
        return pd.DataFrame.from_dict(out, orient="index")

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


def _num(values: pd.Series, key: str) -> float:
    return float(cast(Any, values[key]))


def _trailing(quarters: pd.DataFrame, skip: int) -> pd.Series | None:
    """The sum of four consecutive quarters after the newest ``skip``, or
    ``None`` when four are missing or they span more than about a year."""
    rows = quarters.iloc[skip : skip + 4]
    if len(rows) < 4:
        return None
    ends = pd.to_datetime(rows["period_end"])
    if (ends.iloc[0] - ends.iloc[-1]).days > 300:
        return None
    return rows[list(_INCOME_COLS)].astype(float).sum(skipna=False)

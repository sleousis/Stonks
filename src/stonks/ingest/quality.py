"""Bar validation on ingest (roadmap 12.5).

:class:`BarQualityChecker` looks at one batch of bars (what a source
returned for a ticker) before it reaches the bar store and sorts what it
finds into two kinds:

- **Rejections** (row level, reason codes in :data:`QUARANTINE_REASONS`):
  the pipeline writes these rows to ``quarantined_bars`` instead of the
  bar store. Only rules a real bar cannot break go here: missing or
  non-positive prices, ``high < low``, ``close`` outside ``[low, high]``,
  an earlier copy of a duplicated timestamp (among the copies that pass
  the other rules, so a bad later copy never costs the day), and a one-bar
  spike that the next bar takes back.
- **Stored spikes**: when the batch's first bar takes back a large move
  made by the last *stored* bar (a daily ingest stores one bar at a
  time, so yesterday's bad tick was stored before today's bar showed it
  up), that stored bar is reported in :attr:`BatchQuality.stored_spikes`.
  The pipeline moves it to ``quarantined_bars`` and deletes it from the
  bar store.
- **Warnings** (series level, :data:`WARNING_CODES`): a large move that
  holds, calendar gaps, a stale series, flat-price or zero-volume streaks.
  These can be real (a crash, a halt, an illiquid name), so they are
  reported and alerted on but never drop data.

Splits never count as moves: a return is measured on raw ``close``
corrected by the split ratio on that ex-date (from the lake's
``stock_splits``), or on the vendor's ``adj_close`` when both bars come
from the same batch, whichever explains the move better. With no split
record at all a split still only raises an ``extreme_move`` warning,
because it does not revert.

Calendar gaps need a trading calendar. The checker takes any object with
``sessions(ticker, start, end) -> Iterable[date] | None`` (duck-typed, so
the market-calendar package plugs in without an import here); ``None``
from it, or no such method, skips the rule.
"""

from __future__ import annotations

import json
import math
from collections import Counter
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from typing import TYPE_CHECKING, Any, Literal, Protocol

import numpy as np
import pandas as pd

from stonks.core.interval import Interval
from stonks.ingest.quality_config import DataQualityConfig

if TYPE_CHECKING:
    from stonks.store.lake import DuckDBLake

QuarantineReason = Literal[
    "missing_price",
    "non_positive_price",
    "high_below_low",
    "close_out_of_range",
    "duplicate_timestamp",
    "price_spike",
]
QUARANTINE_REASONS: tuple[str, ...] = QuarantineReason.__args__  # type: ignore[attr-defined]

WarningCode = Literal[
    "extreme_move",
    "calendar_gap",
    "stale_series",
    "flat_price_streak",
    "zero_volume_streak",
    "no_data",
]
WARNING_CODES: tuple[str, ...] = WarningCode.__args__  # type: ignore[attr-defined]

_PRICE_COLS = ("open", "high", "low", "close")
_MAD_TO_SIGMA = 1.4826


class SessionCalendar(Protocol):
    """Trading sessions of ``ticker``'s market between two dates
    (inclusive), or ``None`` when the calendar does not know the market."""

    def sessions(self, ticker: str, start: date, end: date) -> Iterable[date] | None: ...


@dataclass(frozen=True)
class SeriesWarning:
    ticker: str
    code: WarningCode
    count: int
    detail: str = ""


@dataclass
class BatchQuality:
    """Result of one :meth:`BarQualityChecker.check`: ``reasons`` is aligned
    with the checked frame's index and holds comma-joined reason codes
    ("" for a clean row)."""

    reasons: pd.Series
    warnings: list[SeriesWarning] = field(default_factory=list)
    #: ``(ticker, timestamp)`` of stored bars (from ``history``) that the
    #: batch shows to be one-bar spikes.
    stored_spikes: list[tuple[str, pd.Timestamp]] = field(default_factory=list)

    @property
    def rejected_mask(self) -> pd.Series:
        return self.reasons != ""

    @property
    def rejected(self) -> int:
        return int(self.rejected_mask.sum())


class BarQualityChecker:
    """Validates bar batches; see the module doc for the rules."""

    def __init__(
        self, config: DataQualityConfig | None = None, *, calendar: object | None = None
    ) -> None:
        self.config = config or DataQualityConfig()
        self.calendar = calendar

    def check(
        self,
        frame: pd.DataFrame,
        *,
        interval: Interval,
        history: pd.DataFrame | None = None,
        splits: pd.DataFrame | None = None,
        as_of: date | None = None,
        asset_class: str | None = None,
    ) -> BatchQuality:
        """Check ``frame`` (columns ``ticker, timestamp, open, high, low,
        close, adj_close, volume``). ``history`` holds already stored bars
        of the same series (context for the spike rule), ``splits`` the
        series' splits (``ex_date``, ``ratio``; a ``ticker`` column is
        honoured), ``as_of`` the date the batch should reach (stale rule),
        ``asset_class`` the instrument's class (None counts as equity)."""
        reasons = pd.Series("", index=frame.index, dtype=object)
        result = BatchQuality(reasons=reasons)
        if frame.empty or not self.config.enabled:
            return result
        codes: dict[Any, list[str]] = {i: [] for i in frame.index}
        for ticker, group in frame.groupby("ticker", sort=False):
            hist = _for_ticker(history, ticker)
            spl = _for_ticker(splits, ticker)
            self._check_series(
                str(ticker),
                group,
                interval,
                hist,
                spl,
                as_of,
                asset_class or "equity",
                codes,
                result.warnings,
                result.stored_spikes,
            )
        result.reasons = pd.Series(
            {i: ",".join(c) for i, c in codes.items()}, dtype=object
        ).reindex(frame.index)
        return result

    # ---- one series -----------------------------------------------------------

    def _check_series(
        self,
        ticker: str,
        group: pd.DataFrame,
        interval: Interval,
        history: pd.DataFrame | None,
        splits: pd.DataFrame | None,
        as_of: date | None,
        asset_class: str,
        codes: dict[Any, list[str]],
        warnings: list[SeriesWarning],
        stored_spikes: list[tuple[str, pd.Timestamp]],
    ) -> None:
        cfg = self.config
        prices = group[list(_PRICE_COLS)].astype(float)
        missing = prices.isna().any(axis=1)
        non_positive = (prices <= 0).any(axis=1) & ~missing
        if asset_class in cfg.allow_non_positive:
            non_positive &= False
        high, low, close = prices["high"], prices["low"], prices["close"]
        high_below_low = ~missing & (high < low)
        tol = cfg.range_tolerance
        out_of_range = (
            ~missing & ~high_below_low & ((close > high * (1 + tol)) | (close < low * (1 - tol)))
        )
        for mask, reason in (
            (missing, "missing_price"),
            (non_positive, "non_positive_price"),
            (high_below_low, "high_below_low"),
            (out_of_range, "close_out_of_range"),
        ):
            for idx in group.index[mask.to_numpy()]:
                codes[idx].append(reason)
        # Among the copies of a timestamp that pass the row rules, the last
        # one wins; a bad copy is already rejected on its own merit.
        passing = group[[not codes[i] for i in group.index]]
        duplicate = pd.to_datetime(passing["timestamp"]).duplicated(keep="last")
        for idx in passing.index[duplicate.to_numpy()]:
            codes[idx].append("duplicate_timestamp")

        valid = group[[not codes[i] for i in group.index]]
        spikes: list[Any] = []
        moves: list[Any] = []
        if asset_class in cfg.spike_asset_classes:
            spikes, moves, stored = self._spikes(valid, history, splits)
            stored_spikes.extend((ticker, ts) for ts in stored)
        for idx in spikes:
            codes[idx].append("price_spike")
        if moves:
            warnings.append(
                SeriesWarning(
                    ticker, "extreme_move", len(moves), "large move(s) that did not revert"
                )
            )
        clean = valid.drop(index=list(spikes)).sort_values("timestamp")
        self._series_warnings(ticker, clean, interval, as_of, warnings)

    def _spikes(
        self,
        valid: pd.DataFrame,
        history: pd.DataFrame | None,
        splits: pd.DataFrame | None,
    ) -> tuple[list[Any], list[Any], list[pd.Timestamp]]:
        """Batch rows that are one-bar spikes, batch rows whose large move
        holds (warnings), and the timestamp of the last stored bar when the
        batch's first bar takes back its large move."""
        cfg = self.config
        batch = valid.assign(_ts=pd.to_datetime(valid["timestamp"]), _idx=valid.index)
        batch = batch.sort_values("_ts")
        if batch.empty:
            return [], [], []
        parts = [batch[["_ts", "close", "adj_close", "_idx"]]]
        if history is not None and not history.empty:
            hist = history.assign(_ts=pd.to_datetime(history["timestamp"]), _idx=None)
            hist = hist[hist["_ts"] < batch["_ts"].min()]
            hist = hist[(hist["close"] > 0) & hist["close"].notna()]
            parts.insert(0, hist[["_ts", "close", "adj_close", "_idx"]])
        series = pd.concat(parts, ignore_index=True).sort_values("_ts", kind="stable")
        series = series.reset_index(drop=True)
        if len(series) - 1 < cfg.min_history_bars:
            return [], [], []
        returns = _returns(series, splits)
        finite = returns[np.isfinite(returns)]
        if len(finite) < cfg.min_history_bars:
            return [], [], []
        mad = float(np.median(np.abs(finite - np.median(finite))))
        sigma = max(_MAD_TO_SIGMA * mad, cfg.min_sigma)
        threshold = max(cfg.spike_sigmas * sigma, cfg.spike_min_move)
        spikes: list[Any] = []
        moves: list[Any] = []
        stored: list[pd.Timestamp] = []
        idx_col = series["_idx"].tolist()
        skip_next = False
        for i in range(1, len(series)):
            if skip_next:
                skip_next = False
                continue
            idx = idx_col[i]
            r = returns[i]
            if not np.isfinite(r) or abs(r) <= threshold:
                continue
            nxt = returns[i + 1] if i + 1 < len(series) else math.nan
            reverts = np.isfinite(nxt) and abs(r + nxt) < cfg.spike_reversal_tolerance * abs(r)
            if pd.isna(idx):
                # a stored bar: judged only when it is the last one and the
                # batch's first bar takes its move back
                if reverts and not pd.isna(idx_col[i + 1]):
                    stored.append(pd.Timestamp(series["_ts"].iloc[i]))
                    skip_next = True
                continue
            if reverts:
                spikes.append(idx)
                skip_next = True
            else:
                moves.append(idx)
        return spikes, moves, stored

    def _series_warnings(
        self,
        ticker: str,
        clean: pd.DataFrame,
        interval: Interval,
        as_of: date | None,
        warnings: list[SeriesWarning],
    ) -> None:
        cfg = self.config
        if clean.empty:
            return
        volume = clean["volume"]
        zero_run = _longest_run((volume == 0) & volume.notna())
        if zero_run >= cfg.zero_volume_streak:
            warnings.append(
                SeriesWarning(
                    ticker, "zero_volume_streak", zero_run, "consecutive zero-volume bars"
                )
            )
        close = clean["close"].astype(float).to_numpy()
        flat_run = _longest_run(pd.Series(close[1:] == close[:-1])) + 1 if len(close) > 1 else 1
        if flat_run >= cfg.flat_price_streak:
            warnings.append(
                SeriesWarning(ticker, "flat_price_streak", flat_run, "consecutive unchanged closes")
            )
        if interval.is_intraday:
            return
        # Only rows that reach the store count: a day whose only bar was
        # rejected is a gap, and a rejected newest bar does not refresh.
        days = pd.to_datetime(clean["timestamp"]).dt.date
        last = max(days)
        if as_of is not None and (as_of - last).days > cfg.stale_after_days:
            warnings.append(
                SeriesWarning(
                    ticker, "stale_series", (as_of - last).days, f"newest bar {last.isoformat()}"
                )
            )
        gaps = self._calendar_gaps(ticker, min(days), last, set(days))
        if gaps:
            warnings.append(
                SeriesWarning(
                    ticker,
                    "calendar_gap",
                    len(gaps),
                    "missing sessions: " + ", ".join(d.isoformat() for d in gaps[:5]),
                )
            )

    def _calendar_gaps(self, ticker: str, start: date, end: date, present: set[date]) -> list[date]:
        sessions = getattr(self.calendar, "sessions", None)
        if not callable(sessions):
            return []
        expected = sessions(ticker, start, end)
        if expected is None:
            return []
        return sorted(d for d in {_as_date(s) for s in expected} if d not in present)


# ---- run summary ------------------------------------------------------------------


@dataclass
class RunQuality:
    """Quality counters for one ingest run, stored as JSON on
    ``ingest_runs.quality_json``. ``supplied_by`` maps each ticker served
    by a fallback source to that source's id."""

    bars_checked: int = 0
    bars_quarantined: int = 0
    reasons: Counter = field(default_factory=Counter)
    warnings: list[SeriesWarning] = field(default_factory=list)
    supplied_by: dict[str, str] = field(default_factory=dict)

    def add(self, checked: int, batch: BatchQuality) -> None:
        self.bars_checked += checked
        self.bars_quarantined += batch.rejected
        for joined in batch.reasons[batch.rejected_mask]:
            self.reasons.update(joined.split(","))
        self.warnings.extend(batch.warnings)

    @property
    def warned_tickers(self) -> list[str]:
        return sorted({w.ticker for w in self.warnings})

    def to_dict(self) -> dict[str, Any]:
        warning_counts = Counter(w.code for w in self.warnings)
        return {
            "bars_checked": self.bars_checked,
            "bars_quarantined": self.bars_quarantined,
            "reasons": dict(sorted(self.reasons.items())),
            "warnings": dict(sorted(warning_counts.items())),
            "warned_tickers": self.warned_tickers,
            "warning_details": [
                {"ticker": w.ticker, "code": w.code, "count": w.count, "detail": w.detail}
                for w in self.warnings
            ],
            "supplied_by": dict(sorted(self.supplied_by.items())),
        }

    def breaches(self, config: DataQualityConfig) -> list[str]:
        """Human-readable reasons this run should alert (empty: no alert)."""
        out = []
        if config.alert_quarantined_rows and self.bars_quarantined >= config.alert_quarantined_rows:
            out.append(f"{self.bars_quarantined} bar(s) quarantined")
        if config.alert_warned_tickers and len(self.warned_tickers) >= config.alert_warned_tickers:
            out.append(f"{len(self.warned_tickers)} ticker(s) with quality warnings")
        if config.alert_on_fallback and self.supplied_by:
            out.append(f"{len(self.supplied_by)} ticker(s) served by a fallback source")
        return out


# ---- lake persistence -------------------------------------------------------------

_QUARANTINE_COLS = (
    "run_id",
    "ticker",
    "timestamp",
    "interval",
    "open",
    "high",
    "low",
    "close",
    "adj_close",
    "volume",
    "reasons",
    "source",
    "quarantined_at",
)


def quarantine_bars(
    lake: DuckDBLake,
    rows: pd.DataFrame,
    *,
    run_id: int,
    interval: Interval,
    source: str,
) -> int:
    """Append rejected bars (with a ``reasons`` column) to
    ``quarantined_bars``. The table is an audit log: a bar rejected again
    by a later run gets a second row."""
    if rows.empty:
        return 0
    frame = rows.copy()
    frame["run_id"] = run_id
    frame["interval"] = interval.code
    frame["source"] = source
    frame["quarantined_at"] = datetime.now(UTC).replace(tzinfo=None)
    frame["timestamp"] = pd.to_datetime(frame["timestamp"])
    frame = frame[list(_QUARANTINE_COLS)]
    name = "_quarantine_in"
    lake.con.register(name, frame)
    try:
        lake.con.execute(
            f"INSERT INTO quarantined_bars ({', '.join(_QUARANTINE_COLS)}) "
            f"SELECT {', '.join(_QUARANTINE_COLS)} FROM {name}"
        )
    finally:
        lake.con.unregister(name)
    return len(frame)


def record_run_quality(lake: DuckDBLake, run_id: int, quality: RunQuality) -> None:
    lake.con.execute(
        "UPDATE ingest_runs SET quality_json = ? WHERE id = ?",
        [json.dumps(quality.to_dict()), run_id],
    )


def run_quality(lake: DuckDBLake, run_id: int) -> dict[str, Any] | None:
    row = lake.con.execute("SELECT quality_json FROM ingest_runs WHERE id = ?", [run_id]).fetchone()
    if row is None or row[0] is None:
        return None
    return json.loads(row[0])


def quarantined_bars(
    lake: DuckDBLake, *, ticker: str | None = None, run_id: int | None = None
) -> pd.DataFrame:
    where, params = [], []
    if ticker is not None:
        where.append("ticker = ?")
        params.append(ticker)
    if run_id is not None:
        where.append("run_id = ?")
        params.append(run_id)
    clause = f"WHERE {' AND '.join(where)}" if where else ""
    return lake.con.execute(
        f"SELECT * FROM quarantined_bars {clause} ORDER BY ticker, timestamp, id", params
    ).fetchdf()


def splits_frame(lake: DuckDBLake, tickers: Sequence[str]) -> pd.DataFrame:
    """The lake's splits for ``tickers`` as ``ticker, ex_date, ratio``."""
    actions = lake.get_corporate_actions(list(tickers))
    splits = actions[actions["kind"] == "split"]
    return splits.rename(columns={"value": "ratio"})[["ticker", "ex_date", "ratio"]]


def history_before(
    lake: DuckDBLake, ticker: str, interval: Interval, first: datetime, bars: int
) -> pd.DataFrame:
    """Up to about ``bars`` stored bars of the series before ``first``."""
    if bars <= 0:
        return pd.DataFrame()
    span = timedelta(seconds=interval.seconds * bars * 2)
    start = first - max(span, timedelta(days=7))
    stored = lake.get_bars(ticker, interval, start, first - timedelta(microseconds=1))
    return stored.tail(bars)


# ---- helpers ------------------------------------------------------------------------


def _for_ticker(frame: pd.DataFrame | None, ticker: Any) -> pd.DataFrame | None:
    if frame is None or frame.empty or "ticker" not in frame.columns:
        return frame
    return frame[frame["ticker"] == ticker]


def _returns(series: pd.DataFrame, splits: pd.DataFrame | None) -> np.ndarray:
    """Log return of each element vs the previous one (index 0 is NaN),
    split-corrected, or measured on ``adj_close`` inside the batch when
    that explains the move better."""
    close = series["close"].astype(float).to_numpy()
    adj = pd.to_numeric(series["adj_close"], errors="coerce").to_numpy(dtype=float)
    in_batch = series["_idx"].notna().to_numpy()
    days = [ts.date() for ts in series["_ts"]]
    split_events = []
    if splits is not None and not splits.empty:
        split_events = [
            (_as_date(d), float(r))
            for d, r in zip(splits["ex_date"], splits["ratio"], strict=True)
            if r and r > 0
        ]
    out = np.full(len(series), np.nan)
    with np.errstate(divide="ignore", invalid="ignore"):
        for i in range(1, len(series)):
            if not (close[i] > 0 and close[i - 1] > 0):
                # no log return across a zero or negative price (futures,
                # yields); the rule has nothing to measure there
                continue
            raw = math.log(close[i] / close[i - 1])
            factor = 1.0
            for day, ratio in split_events:
                if days[i - 1] < day <= days[i]:
                    factor *= ratio
            best = raw + math.log(factor)
            if in_batch[i] and in_batch[i - 1] and adj[i] > 0 and adj[i - 1] > 0:
                adjusted = math.log(adj[i] / adj[i - 1])
                if abs(adjusted) < abs(best):
                    best = adjusted
            out[i] = best
    return out


def _longest_run(mask: pd.Series) -> int:
    best = run = 0
    for flag in mask.to_numpy():
        run = run + 1 if flag else 0
        best = max(best, run)
    return best


def _as_date(value: Any) -> date:
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    return pd.Timestamp(value).date()

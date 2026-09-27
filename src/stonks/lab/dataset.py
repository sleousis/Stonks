"""LabDataset — the view over the lake that a tuner / survival test / backtest
operates against. Defines universe, full window, and train/val split.

Embargo (BL-20, principle P9)
-----------------------------
``embargo_bars`` trading bars separate the train window from the validation
window: ``val_window`` starts that many bars after ``train_end``, so
serially correlated features and labels that span bars cannot leak across
the boundary. The train window itself never moves (the tuner and ``fit``
see the same data as without an embargo); the bars in the gap are scored
by nobody.

A strategy whose labels look ``label_horizon_bars`` bars ahead needs an
embargo at least that long: the labels of the last train bars read prices
inside the gap, never inside the validation window (this is the purge of
López de Prado's purged k-fold, done by construction). The effective
embargo is ``max(embargo_bars, label_horizon_bars)``; ``for_strategy``
returns the dataset with it applied, and :func:`scoring_window` is what
validation-style survival tests call.

Data tickers (RS-01)
--------------------
``reference_tickers`` are tickers a strategy reads but never trades: a
reference market, an index filter, a regime condition's ticker.
``for_strategy`` adds the strategy's ``data_tickers()``. :func:`data_tickers`
is the universe plus these plus the benchmark ticker, and it is what every
worker snapshot and every permuted or perturbed lake copies, so a run gives
the same answer on any worker count and a modified lake never silently
drops a reference.

Training segments (BL-45)
-------------------------
Cross-validation folds train on data either side of a test block.
``train_segments`` holds those non-contiguous, purged training windows and
``train_windows`` returns them (the single ``train_window`` when unset).
A strategy that can fit on several windows reads ``train_windows``. One
that only reads ``train_window`` then sees the longest segment, so it
never trains on a test block. A dataset with segments is a CV fold: its
tests score explicit windows, so it has no validation window of its own
to check.

Bars are converted to calendar days through the exchange-session calendar
(``backtest.calendar.EXCHANGE_SESSIONS``: 252 sessions a year): the bars
become sessions, the sessions become calendar days at the yearly average
of 365.25 / 252 (weekends and holidays included), plus a three-day pad so
a short embargo can never fall entirely on a weekend. The conversion is
deliberately conservative: a 24/7 (crypto) universe gets a longer gap in
bars than asked, never a shorter one.

Windows by session (roadmap 21.3.1)
-----------------------------------
An intraday strategy trades inside one session, so its train and
validation windows should hold whole sessions. ``sessions`` lists the
trading sessions of the universe (``with_sessions`` reads them from the
lake: the UTC days on which any universe ticker has a bar at the
dataset's interval). With sessions set:

- ``train_ratio`` splits the sessions inside the window, not calendar
  days, and always leaves at least one validation session
- the embargo is whole sessions (:func:`bars_to_sessions`: the sessions
  that ``embargo_bars`` bars fill on the 6.5-hour exchange session, at
  least one for any embargo), skipped after the last train session
- walk-forward folds count sessions too (``lab.survival.walk_forward``)

The lab turns this on for every intraday dataset (``lab.universe_data.
prepare_dataset``). A dataset whose lake holds no bars keeps the calendar
split. Sessions are plain dates, so a dataset with its lake detached still
splits the same way in a worker process.
"""

from __future__ import annotations

import dataclasses
import math
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta
from typing import TYPE_CHECKING, Any, Literal

from stonks.backtest.benchmark import AUTO_BENCHMARK_TICKER, normalize_spec
from stonks.backtest.calendar import EXCHANGE_SESSIONS
from stonks.core.interval import Interval
from stonks.strategies.base import strategy_data_tickers

if TYPE_CHECKING:  # pragma: no cover
    from stonks.backtest.costs import CostModelSettings
    from stonks.backtest.fills import ExecutionSettings
    from stonks.backtest.report import BacktestReport
    from stonks.backtest.shorting import ShortingSettings
    from stonks.portfolio.settings import ConstructionSettings
    from stonks.store.lake import DuckDBLake

#: Which window a validation-style survival test scores.
ScoringWindow = Literal["val", "full"]

_DAYS_PER_YEAR = 365.25
#: Calendar days added to any non-zero embargo (a weekend plus one).
_EMBARGO_PAD_DAYS = 3


def embargo_calendar_days(bars: int, interval: Interval) -> int:
    """Calendar days that hold at least ``bars`` bars of ``interval`` on
    the exchange-session calendar (see the module doc); 0 for no bars."""
    if bars < 0:
        raise ValueError(f"embargo bars must be >= 0, got {bars}")
    if bars == 0:
        return 0
    sessions_per_year = EXCHANGE_SESSIONS.sessions_per_year
    sessions = math.ceil(
        round(bars * sessions_per_year / EXCHANGE_SESSIONS.periods_per_year(interval), 9)
    )
    return math.ceil(round(sessions * _DAYS_PER_YEAR / sessions_per_year, 9)) + _EMBARGO_PAD_DAYS


def bars_to_sessions(bars: int, interval: Interval) -> int:
    """Whole exchange sessions that hold ``bars`` bars of ``interval`` (see
    the module doc): 390 one-minute bars fill one session, a daily bar is
    one session. 0 for no bars."""
    if bars < 0:
        raise ValueError(f"bars must be >= 0, got {bars}")
    if bars == 0:
        return 0
    per_session = EXCHANGE_SESSIONS.periods_per_year(interval) / EXCHANGE_SESSIONS.sessions_per_year
    return max(1, math.ceil(round(bars / per_session, 9)))


def session_dates(
    lake: Any, tickers: Iterable[str], interval: Interval, start: date, end: date
) -> tuple[date, ...]:
    """The UTC days in ``[start, end]`` on which any of ``tickers`` has a
    bar at ``interval``, sorted (the sessions of the module doc)."""
    days: set[date] = set()
    lo = datetime.combine(start, time.min)
    hi = datetime.combine(end, time.max)
    for ticker in dict.fromkeys(tickers):
        frame = lake.get_bars(ticker, interval, start=lo, end=hi)
        if frame is None or frame.empty:
            continue
        stamps = frame["timestamp"]
        days.update(d for d in (ts.date() for ts in stamps) if start <= d <= end)
    return tuple(sorted(days))


@dataclass
class LabDataset:
    lake: DuckDBLake
    universe: list[str] = field(default_factory=list)
    start: date = date(2000, 1, 1)
    end: date = field(default_factory=date.today)
    train_ratio: float = 0.7
    #: Bar interval the tests/tuner should fetch from the lake. Daily by default;
    #: intraday scenarios pass e.g. ``Interval.MIN_5`` or ``Interval.HOUR_1``.
    interval: Interval = field(default_factory=lambda: Interval.DAY_1)
    #: Explicit last day of the train window; overrides ``train_ratio``.
    #: Walk-forward folds set it so train/test boundaries are exact.
    train_end: date | None = None
    #: Transaction costs for every backtest on this dataset (the CLI fills
    #: it from ``[backtest.costs]``). ``None`` means zero costs.
    costs: CostModelSettings | None = None
    #: Trading bars skipped between the train and the validation window
    #: (see the module doc). 0 keeps the validation window starting the
    #: day after ``train_end``.
    embargo_bars: int = 0
    #: Benchmark spec every backtest on this dataset compares against
    #: (``backtest.benchmark``: ``"auto"``, ``"EW"``, a ticker, ``"none"``).
    benchmark: str = "auto"
    #: Fill model and settlement of every backtest's simulated broker
    #: (``[backtest.execution]``). ``None``: immediate fills, no settlement.
    execution: ExecutionSettings | None = None
    #: The production construction pipeline for every backtest
    #: (``[backtest.construction]``). ``None``: each strategy decides alone.
    construction: ConstructionSettings | None = None
    #: The stored universe this dataset was built from (roadmap 10.5). With
    #: an empty ``universe`` the lab resolves its members over the window
    #: (``lab.universe_data``); the preflight checks membership against it.
    universe_id: str | None = None
    #: Tickers the strategy reads but never trades (see the module doc).
    #: ``for_strategy`` fills it from the strategy's ``data_tickers()``.
    reference_tickers: tuple[str, ...] = ()
    #: Non-contiguous training windows of a CV fold (see the module doc).
    #: Empty for an ordinary dataset.
    train_segments: tuple[tuple[date, date], ...] = ()
    #: Short selling for every backtest on this dataset (roadmap 16.4): the
    #: margin model and borrow fees. ``None`` (the default): long-only.
    shorting: ShortingSettings | None = None
    #: Trading sessions of the universe (see the module doc). Empty: the
    #: windows split by calendar days.
    sessions: tuple[date, ...] = ()
    #: Stitched walk-forward OOS backtest, set by the walk-forward test for
    #: the tests after it (``mc_trades``). Never copied by ``replace``.
    stitched_oos_report: BacktestReport | None = field(
        default=None, init=False, repr=False, compare=False
    )

    def __post_init__(self) -> None:
        # RS-17: both windows must be non-empty and in order.
        if not 0.0 < self.train_ratio < 1.0:
            raise ValueError(f"train_ratio must lie in (0, 1), got {self.train_ratio}")
        if self.end <= self.start:
            raise ValueError(f"end {self.end} must be after start {self.start}")
        if self.embargo_bars < 0:
            raise ValueError(f"embargo_bars must be >= 0, got {self.embargo_bars}")
        if self.train_end is not None and not (self.start <= self.train_end < self.end):
            raise ValueError(
                f"train_end {self.train_end} must fall in [{self.start}, {self.end}) "
                "so both the train and the validation window are non-empty"
            )
        if self.train_segments:
            self._check_segments()
            return
        if self.sessions and len(self.window_sessions) < 2:
            raise ValueError(
                f"the window {self.start}..{self.end} holds "
                f"{len(self.window_sessions)} trading session(s); a split by session needs 2"
            )
        if self.embargo_bars > 0 and self.val_window[0] > self.end:
            raise ValueError(
                f"an embargo of {self.embargo_bars} bars after {self.train_window[1]} "
                f"leaves no validation window before {self.end}"
            )
        if self.val_window[0] > self.end:
            raise ValueError(
                f"the window {self.start}..{self.end} is too short for train_ratio "
                f"{self.train_ratio}: it leaves no validation window"
            )

    def _check_segments(self) -> None:
        previous_end: date | None = None
        for lo, hi in self.train_segments:
            if not self.start <= lo <= hi <= self.end:
                raise ValueError(f"train segment {lo}..{hi} must lie in [{self.start}, {self.end}]")
            if previous_end is not None and lo <= previous_end:
                raise ValueError("train segments must be sorted and must not overlap")
            previous_end = hi

    @property
    def train_windows(self) -> tuple[tuple[date, date], ...]:
        """Every training window: ``train_segments`` when set, else the
        one ``train_window``."""
        return self.train_segments or (self.train_window,)

    def with_train_segments(self, segments: Iterable[tuple[date, date]]) -> LabDataset:
        """This dataset as a CV fold that trains on ``segments``."""
        return dataclasses.replace(self, train_segments=tuple(segments))

    @property
    def window_sessions(self) -> tuple[date, ...]:
        """The ``sessions`` inside ``[start, end]`` (empty without sessions)."""
        return tuple(s for s in self.sessions if self.start <= s <= self.end)

    def with_sessions(self, sessions: Iterable[date] | None = None) -> LabDataset:
        """This dataset split by session (see the module doc): ``sessions``,
        or the days the universe has bars in the lake. Itself when there
        are none."""
        found = (
            tuple(sorted(set(sessions)))
            if sessions is not None
            else session_dates(self.lake, self.universe, self.interval, self.start, self.end)
        )
        if not found or found == self.sessions:
            return self
        return dataclasses.replace(self, sessions=found)

    @property
    def train_window(self) -> tuple[date, date]:
        if self.train_segments:
            # the longest segment (the latest on a tie): a strategy that
            # reads one window never sees a test block
            return max(self.train_segments, key=lambda seg: ((seg[1] - seg[0]).days, seg[0]))
        if self.train_end is not None:
            return self.start, self.train_end
        window = self.window_sessions
        if len(window) >= 2:
            k = min(len(window) - 1, max(1, int(len(window) * self.train_ratio)))
            return self.start, window[k - 1]
        span_days = (self.end - self.start).days
        train_days = max(1, int(span_days * self.train_ratio))
        return self.start, self.start + timedelta(days=train_days)

    @property
    def val_window(self) -> tuple[date, date]:
        _, train_end = self.train_window
        if self.sessions:
            after = [s for s in self.window_sessions if s > train_end]
            skip = bars_to_sessions(self.embargo_bars, self.interval)
            if len(after) <= skip:  # no session left: an empty window
                return self.end + timedelta(days=1), self.end
            return after[skip], self.end
        gap = embargo_calendar_days(self.embargo_bars, self.interval)
        start = train_end + timedelta(days=1 + gap)
        return start, self.end

    @property
    def full_window(self) -> tuple[date, date]:
        return self.start, self.end

    def effective_embargo_bars(self, strategy: Any) -> int:
        """``max(embargo_bars, strategy.label_horizon_bars)`` (0 when the
        strategy declares no label horizon)."""
        horizon = int(getattr(strategy, "label_horizon_bars", 0) or 0)
        return max(self.embargo_bars, horizon)

    def for_strategy(self, strategy: Any) -> LabDataset:
        """This dataset with the embargo ``strategy`` needs and its data
        tickers added to ``reference_tickers`` (itself when neither
        changes anything)."""
        embargo = self.effective_embargo_bars(strategy)
        refs = self.with_references(strategy_data_tickers(strategy)).reference_tickers
        if embargo == self.embargo_bars and refs == self.reference_tickers:
            return self
        return dataclasses.replace(self, embargo_bars=embargo, reference_tickers=refs)

    def with_references(self, tickers: Iterable[str]) -> LabDataset:
        """This dataset with ``tickers`` added to ``reference_tickers``
        (universe members are left out, order is kept)."""
        members = set(self.universe)
        refs = tuple(
            dict.fromkeys([*self.reference_tickers, *(t for t in tickers if t not in members)])
        )
        if refs == self.reference_tickers:
            return self
        return dataclasses.replace(self, reference_tickers=refs)

    def prices_on(self, as_of: date) -> dict[str, float]:
        df = self.lake.sql(
            "SELECT ticker, close FROM prices WHERE ticker = ANY(?) AND date = ?",
            [list(self.universe), as_of],
        )
        return {row.ticker: float(row.close) for row in df.itertuples(index=False)}


def data_tickers(context: Any, extra: Iterable[str] = ()) -> list[str]:
    """Every ticker whose data a run on ``context`` reads: the universe,
    then ``reference_tickers``, ``extra`` and the benchmark ticker
    (``"auto"`` names :data:`~stonks.backtest.benchmark.AUTO_BENCHMARK_TICKER`,
    ``"EW"`` and ``"none"`` name none). Works on any context with a
    ``universe``. A ticker with no bars in the lake is harmless: copies
    simply hold nothing for it."""
    tickers = [
        *context.universe,
        *getattr(context, "reference_tickers", ()),
        *extra,
    ]
    bench = normalize_spec(getattr(context, "benchmark", None))
    if bench == "auto":
        tickers.append(AUTO_BENCHMARK_TICKER)
    elif bench not in (None, "ew"):
        tickers.append(bench)
    return list(dict.fromkeys(str(t) for t in tickers if t))


def scoring_window(context: Any, strategy: Any, window: ScoringWindow = "val") -> tuple[date, date]:
    """The window a validation-style survival test backtests: the
    validation window embargoed for ``strategy`` (``"val"``, the default;
    never any bar the tuner saw), or the whole dataset (``"full"``, the
    pre-BL-21 behaviour). Works on any context with ``val_window`` /
    ``full_window``; ``for_strategy`` is used when present."""
    if window == "full":
        return context.full_window
    if window != "val":
        raise ValueError(f"window must be 'val' or 'full', got {window!r}")
    for_strategy = getattr(context, "for_strategy", None)
    if callable(for_strategy):
        context = for_strategy(strategy)
    return context.val_window

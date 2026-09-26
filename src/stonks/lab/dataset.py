"""LabDataset — the view over the lake that a tuner / survival test / backtest
operates against. Defines universe, full window, and train/val split.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, timedelta
from typing import TYPE_CHECKING

from stonks.core.interval import Interval

if TYPE_CHECKING:  # pragma: no cover
    from stonks.backtest.costs import CostModelSettings
    from stonks.store.lake import DuckDBLake


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

    def __post_init__(self) -> None:
        if self.train_end is not None and not (self.start <= self.train_end < self.end):
            raise ValueError(
                f"train_end {self.train_end} must fall in [{self.start}, {self.end}) "
                "so both the train and the validation window are non-empty"
            )

    @property
    def train_window(self) -> tuple[date, date]:
        if self.train_end is not None:
            return self.start, self.train_end
        span_days = (self.end - self.start).days
        train_days = max(1, int(span_days * self.train_ratio))
        return self.start, self.start + timedelta(days=train_days)

    @property
    def val_window(self) -> tuple[date, date]:
        _, train_end = self.train_window
        start = train_end + timedelta(days=1)
        return start, self.end

    @property
    def full_window(self) -> tuple[date, date]:
        return self.start, self.end

    def prices_on(self, as_of: date) -> dict[str, float]:
        df = self.lake.sql(
            "SELECT ticker, close FROM prices WHERE ticker = ANY(?) AND date = ?",
            [list(self.universe), as_of],
        )
        return {row.ticker: float(row.close) for row in df.itertuples(index=False)}

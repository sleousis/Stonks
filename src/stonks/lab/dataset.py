"""LabDataset — the view over the lake that a tuner / survival test / backtest
operates against. Defines universe, full window, and train/val split.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, timedelta
from typing import TYPE_CHECKING

if TYPE_CHECKING:  # pragma: no cover
    from stonks.store.lake import DuckDBLake


@dataclass
class LabDataset:
    lake: DuckDBLake
    universe: list[str] = field(default_factory=list)
    start: date = date(2000, 1, 1)
    end: date = date.today
    train_ratio: float = 0.7

    @property
    def train_window(self) -> tuple[date, date]:
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

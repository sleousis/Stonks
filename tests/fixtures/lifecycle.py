"""Cheap retrainable strategies for the model lifecycle tests (roadmap 22.6).

Module scope so the registry and spawned workers re-import them by class
path. ``MeanFit`` learns the mean close of its ticker over the training
window and saves it; ``FailingFit`` always raises in ``fit``.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from stonks.strategies.examples.buy_and_hold import BuyAndHold

_FILE = "fitted.json"


class MeanFit(BuyAndHold):
    id = "mean_fit"
    parallel_scoring = False

    def __init__(self, params: Any) -> None:
        super().__init__(params)
        self.mean_close: float | None = None
        self.train_window: tuple[str, str] | None = None

    def fit(self, dataset: Any) -> None:
        start, end = dataset.train_window
        df = dataset.lake.sql(
            "SELECT close FROM prices WHERE ticker = ? AND date BETWEEN ? AND ?",
            [self.params["ticker"], start, end],
        )
        if df.empty:
            raise ValueError("no prices in the training window")
        self.mean_close = float(df["close"].mean())
        self.train_window = (start.isoformat(), end.isoformat())

    def fitted_state(self) -> dict[str, Any]:
        return {"mean_close": self.mean_close, "train_window": self.train_window}

    def save(self, path: Path) -> None:
        super().save(path)
        if self.mean_close is not None:
            (Path(path) / _FILE).write_text(json.dumps(self.fitted_state()))

    @classmethod
    def load(cls, path: Path) -> MeanFit:
        instance: MeanFit = super().load(path)  # type: ignore[assignment]
        fitted = Path(path) / _FILE
        if fitted.exists():
            state = json.loads(fitted.read_text())
            instance.mean_close = state["mean_close"]
            instance.train_window = tuple(state["train_window"])  # type: ignore[assignment]
        return instance


class FailingFit(MeanFit):
    id = "failing_fit"

    def fit(self, dataset: Any) -> None:
        raise ValueError("too few trades to fit")


class ForecastingMeanFit(MeanFit):
    """``MeanFit`` that forecasts P(close above its fitted mean) at 0.7 for
    its ticker, one event per day, known the next day (roadmap 23.9)."""

    id = "forecasting_mean_fit"

    def forecast_probability(self, ticker: str, as_of: Any, lake: Any) -> Any:
        from stonks.core.forecasts import ProbabilityForecast

        if ticker != self.params["ticker"] or self.mean_close is None:
            return None
        return ProbabilityForecast(event_key=str(as_of), probability=0.7)

    def forecast_outcome(self, ticker: str, event_key: str, as_of: Any, lake: Any) -> bool | None:
        if str(as_of) <= event_key:
            return None
        df = lake.sql(
            "SELECT close FROM prices WHERE ticker = ? AND date <= ? ORDER BY date DESC LIMIT 1",
            [ticker, as_of],
        )
        return bool(float(df["close"].iloc[0]) > (self.mean_close or 0.0))

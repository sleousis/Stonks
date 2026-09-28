"""Live calibration of classifier model versions (roadmap 23.9).

Every model version that runs as a model book and forecasts probabilities
(:class:`~stonks.core.forecasts.ProbabilityForecaster`) records each
forecast event in ``model_forecasts``, once per event. Each tick resolves
the open events it can: the strategy says whether the event happened.
:func:`calibration_report` scores one version: the Brier score, the Brier
score of always forecasting the base rate (the bar to beat), the
reliability table and the expected calibration error.

The tick calls :func:`track_calibration` after the version books. It never
raises into the tick: a forecaster that fails is logged and skipped.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from typing import Any

import numpy as np

from stonks.core.forecasts import ProbabilityForecaster
from stonks.logging import get_logger
from stonks.stats.calibration import (
    ReliabilityBin,
    brier_score,
    expected_calibration_error,
    reliability,
)
from stonks.store.state import SqliteState

_log = get_logger("stonks.lifecycle.calibration")

__all__ = [
    "CalibrationReport",
    "calibration_report",
    "forecasts_recorded",
    "record_forecasts",
    "resolve_forecasts",
    "track_calibration",
]


def forecasts_recorded(state: SqliteState) -> bool:
    """The state has ``model_forecasts`` (migration 053)."""
    return bool(
        state.sql("SELECT 1 FROM sqlite_master WHERE type='table' AND name='model_forecasts'")
    )


def record_forecasts(
    state: SqliteState,
    strategy_id: str,
    version: int,
    model: ProbabilityForecaster,
    tickers: Iterable[str],
    as_of: date,
    lake: Any,
) -> int:
    """Store today's forecast for each ticker; an event already stored is
    kept as first seen. Returns the new rows."""
    added = 0
    now = datetime.now(UTC).isoformat(timespec="seconds")
    for ticker in tickers:
        forecast = model.forecast_probability(ticker, as_of, lake)
        if forecast is None:
            continue
        cur = state.execute(
            "INSERT OR IGNORE INTO model_forecasts"
            " (strategy_id, version, ticker, event_key, as_of, probability, recorded_at)"
            " VALUES (?, ?, ?, ?, ?, ?, ?)",
            [
                strategy_id,
                int(version),
                ticker,
                forecast.event_key,
                as_of.isoformat(),
                float(forecast.probability),
                now,
            ],
        )
        added += max(int(getattr(cur, "rowcount", 0) or 0), 0)
    return added


def resolve_forecasts(
    state: SqliteState,
    strategy_id: str,
    version: int,
    model: ProbabilityForecaster,
    as_of: date,
    lake: Any,
) -> int:
    """Settle the version's open events whose outcome is known at
    ``as_of``. Returns how many were settled."""
    rows = state.sql(
        "SELECT ticker, event_key FROM model_forecasts"
        " WHERE strategy_id = ? AND version = ? AND outcome IS NULL AND as_of <= ?",
        [strategy_id, int(version), as_of.isoformat()],
    )
    settled = 0
    for row in rows:
        outcome = model.forecast_outcome(row["ticker"], row["event_key"], as_of, lake)
        if outcome is None:
            continue
        state.execute(
            "UPDATE model_forecasts SET outcome = ?, resolved_on = ?"
            " WHERE strategy_id = ? AND version = ? AND ticker = ? AND event_key = ?",
            [
                int(bool(outcome)),
                as_of.isoformat(),
                strategy_id,
                int(version),
                row["ticker"],
                row["event_key"],
            ],
        )
        settled += 1
    return settled


def track_calibration(
    state: SqliteState,
    lake: Any,
    models: Sequence[tuple[str, int, Any]],
    tickers: Sequence[str],
    as_of: date,
) -> list[dict[str, Any]]:
    """Record and resolve forecasts for each ``(strategy_id, version,
    strategy)`` that forecasts probabilities. A summary row per version;
    never raises."""
    if not forecasts_recorded(state):
        return []
    out: list[dict[str, Any]] = []
    for strategy_id, version, model in models:
        if not isinstance(model, ProbabilityForecaster):
            continue
        try:
            settled = resolve_forecasts(state, strategy_id, version, model, as_of, lake)
            added = record_forecasts(state, strategy_id, version, model, tickers, as_of, lake)
        except Exception as exc:  # a model's bug must not fail the tick
            _log.warning(
                "calibration.track_failed",
                strategy_id=strategy_id,
                version=version,
                error=f"{type(exc).__name__}: {exc}",
            )
            continue
        out.append(
            {"strategy_id": strategy_id, "version": version, "recorded": added, "resolved": settled}
        )
    return out


@dataclass(frozen=True)
class CalibrationReport:
    strategy_id: str
    version: int
    n_forecasts: int
    n_resolved: int
    #: ``None`` until an outcome is known.
    brier: float | None = None
    #: Brier score of always forecasting the observed base rate.
    brier_base_rate: float | None = None
    base_rate: float | None = None
    mean_forecast: float | None = None
    ece: float | None = None
    bins: list[ReliabilityBin] = field(default_factory=list)

    @property
    def skill(self) -> float | None:
        """``1 - brier / brier_base_rate``: above 0 beats the base rate."""
        if self.brier is None or not self.brier_base_rate:
            return None
        return 1.0 - self.brier / self.brier_base_rate

    def as_dict(self) -> dict[str, Any]:
        return {
            "strategy_id": self.strategy_id,
            "version": self.version,
            "n_forecasts": self.n_forecasts,
            "n_resolved": self.n_resolved,
            "brier": self.brier,
            "brier_base_rate": self.brier_base_rate,
            "skill": self.skill,
            "base_rate": self.base_rate,
            "mean_forecast": self.mean_forecast,
            "ece": self.ece,
            "bins": [
                {
                    "lower": b.lower,
                    "upper": b.upper,
                    "count": b.count,
                    "mean_forecast": b.mean_forecast,
                    "observed_rate": b.observed_rate,
                }
                for b in self.bins
            ],
        }


def calibration_report(
    state: SqliteState, strategy_id: str, version: int, n_bins: int = 10
) -> CalibrationReport:
    """Brier score and reliability of one version's resolved forecasts."""
    if not forecasts_recorded(state):
        return CalibrationReport(strategy_id, int(version), 0, 0)
    rows = state.sql(
        "SELECT probability, outcome FROM model_forecasts WHERE strategy_id = ? AND version = ?",
        [strategy_id, int(version)],
    )
    resolved = [(r["probability"], r["outcome"]) for r in rows if r["outcome"] is not None]
    if not resolved:
        return CalibrationReport(strategy_id, int(version), len(rows), 0)
    p = np.array([r[0] for r in resolved], dtype=float)
    y = np.array([r[1] for r in resolved], dtype=float)
    rate = float(y.mean())
    bins = reliability(p, y, n_bins)
    return CalibrationReport(
        strategy_id=strategy_id,
        version=int(version),
        n_forecasts=len(rows),
        n_resolved=len(resolved),
        brier=brier_score(p, y),
        brier_base_rate=rate * (1.0 - rate),
        base_rate=rate,
        mean_forecast=float(p.mean()),
        ece=expected_calibration_error(bins),
        bins=bins,
    )

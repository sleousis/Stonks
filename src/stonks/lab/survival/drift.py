"""Drift test: compare feature distributions on train vs val window via PSI.

``extract_features`` failures are counted (``errors`` / ``attempts`` in
metrics). If every call errored the test fails — a strategy whose feature
code always crashes must not pass as "no features". A strategy that
returns no features without erroring (typical rule-based) is skipped
and passes.
"""

from __future__ import annotations

import contextlib
import math
import random
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import date, timedelta
from typing import ClassVar

from stonks.core.protocols import Strategy, SurvivalReport
from stonks.lab.dataset import LabDataset
from stonks.logging import get_logger

_log = get_logger("stonks.lab.survival.drift")


class DriftTest:
    id = "drift"

    #: Plain words for each option, shown by the console's options editor.
    option_help: ClassVar[dict[str, str]] = {
        "max_psi": "Largest shift in feature values between tuning and held-out data (PSI) that passes.",
        "sample_dates": "How many dates to sample in each window.",
        "bins": "How many buckets to sort feature values into.",
    }

    def __init__(
        self,
        max_psi: float = 0.25,
        sample_dates: int = 20,
        bins: int = 5,
        seed: int | None = 7,
    ) -> None:
        self._max_psi = max_psi
        self._sample_dates = sample_dates
        self._bins = bins
        self._seed = seed

    def run(self, strategy: Strategy, context: LabDataset) -> SurvivalReport:
        rng = random.Random(self._seed)
        train_dates = _sample_dates(context.train_window, self._sample_dates, rng)
        val_dates = _sample_dates(context.val_window, self._sample_dates, rng)

        train = _collect_features(strategy, train_dates, context)
        val = _collect_features(strategy, val_dates, context)
        train_features, val_features = train.features, val.features
        attempts = train.attempts + val.attempts
        errors = train.errors + val.errors
        error_metrics = {"errors": float(errors), "attempts": float(attempts)}

        if attempts > 0 and errors == attempts:
            first = train.first_error or val.first_error
            _log.warning("drift.all_attempts_failed", attempts=attempts, first_error=first)
            return SurvivalReport(
                test_id=self.id,
                passed=False,
                metrics={
                    "max_psi": 0.0,
                    "mean_psi": 0.0,
                    "features_compared": 0.0,
                    **error_metrics,
                },
                notes=f"extract_features failed on all {attempts} attempts: {first}",
            )

        psis: dict[str, float] = {}
        n_values = n_nan = 0
        for name in sorted(set(train_features) & set(val_features)):
            t_vals = train_features[name]
            v_vals = val_features[name]
            n_values += len(t_vals) + len(v_vals)
            n_nan += len(t_vals) + len(v_vals) - len(_finite(t_vals)) - len(_finite(v_vals))
            if len(_finite(t_vals)) < 2 or len(_finite(v_vals)) < 2:
                continue
            psis[name] = _psi(t_vals, v_vals, bins=self._bins)
        error_metrics["nan_share"] = n_nan / n_values if n_values else 0.0

        if not psis:
            return SurvivalReport(
                test_id=self.id,
                passed=True,
                metrics={
                    "max_psi": 0.0,
                    "mean_psi": 0.0,
                    "features_compared": 0.0,
                    **error_metrics,
                },
                notes="no features produced by the strategy; drift test skipped",
            )

        max_psi = max(psis.values())
        mean_psi = sum(psis.values()) / len(psis)
        metrics = {
            "max_psi": max_psi,
            "mean_psi": mean_psi,
            "features_compared": float(len(psis)),
            **error_metrics,
        }
        return SurvivalReport(
            test_id=self.id,
            passed=max_psi <= self._max_psi,
            metrics=metrics,
            notes=", ".join(f"{k}={v:.3f}" for k, v in psis.items()),
        )


def _sample_dates(window: tuple[date, date], n: int, rng: random.Random) -> list[date]:
    start, end = window
    span = (end - start).days
    if span <= 0:
        return []
    return [start + timedelta(days=rng.randint(0, span)) for _ in range(n)]


@dataclass
class _Collected:
    features: dict[str, list[float]] = field(default_factory=dict)
    attempts: int = 0
    errors: int = 0
    first_error: str | None = None


def _collect_features(
    strategy: Strategy,
    dates: Sequence[date],
    context: LabDataset,
) -> _Collected:
    out = _Collected()
    for d in dates:
        for ticker in context.universe:
            out.attempts += 1
            try:
                f = strategy.extract_features(ticker, d, context.lake)
            except Exception as exc:
                out.errors += 1
                if out.first_error is None:
                    out.first_error = f"{type(exc).__name__}: {exc}"
                continue
            for name, value in f.values.items():
                with contextlib.suppress(TypeError, ValueError):
                    out.features.setdefault(name, []).append(float(value))
    if out.errors:
        _log.warning(
            "drift.extract_features.errors",
            errors=out.errors,
            attempts=out.attempts,
            first_error=out.first_error,
        )
    return out


def _finite(values: Sequence[float]) -> list[float]:
    return [v for v in values if math.isfinite(v)]


def _psi(expected: Sequence[float], actual: Sequence[float], bins: int) -> float:
    """PSI over the finite values only (RS-11): a NaN or inf never picks a
    bin edge or lands in a bin. The NaN share is reported separately."""
    expected, actual = _finite(expected), _finite(actual)
    if not expected or not actual:
        return 0.0
    lo = min(min(expected), min(actual))
    hi = max(max(expected), max(actual))
    if not math.isfinite(lo) or not math.isfinite(hi) or hi <= lo:
        return 0.0
    edges = [lo + (hi - lo) * i / bins for i in range(bins + 1)]
    e_hist = _histogram(expected, edges)
    a_hist = _histogram(actual, edges)
    e_frac = [max(c / len(expected), 1e-6) for c in e_hist]
    a_frac = [max(c / len(actual), 1e-6) for c in a_hist]
    return sum((a - e) * math.log(a / e) for a, e in zip(a_frac, e_frac, strict=False))


def _histogram(values: Sequence[float], edges: Sequence[float]) -> list[int]:
    counts = [0] * (len(edges) - 1)
    for v in values:
        for i in range(len(counts)):
            if v <= edges[i + 1] or i == len(counts) - 1:
                counts[i] += 1
                break
    return counts

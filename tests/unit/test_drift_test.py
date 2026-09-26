"""Unit tests for DriftTest error accounting (no lake needed: the fake
strategies ignore it)."""

from __future__ import annotations

from datetime import date

from stonks.core.types import Features
from stonks.lab.dataset import LabDataset
from stonks.lab.survival.drift import DriftTest
from stonks.strategies.base import BaseStrategy


class _Crashing(BaseStrategy):
    id = "crashing_fake"

    def extract_features(self, ticker, as_of, lake):
        raise RuntimeError("feature pipeline exploded")

    def estimate_return(self, ticker, as_of, lake):  # pragma: no cover
        return None

    def decide(self, my_picks, portfolio, prices, as_of):  # pragma: no cover
        return []


class _Featureless(_Crashing):
    id = "featureless_fake"

    def extract_features(self, ticker, as_of, lake):
        return Features(values={})


class _SometimesCrashing(_Crashing):
    id = "sometimes_crashing_fake"

    def __init__(self, params):
        super().__init__(params)
        self._calls = 0

    def extract_features(self, ticker, as_of, lake):
        self._calls += 1
        if self._calls % 4 == 0:
            raise RuntimeError("flaky")
        return Features(values={"x": float(self._calls % 3)})


def _dataset() -> LabDataset:
    return LabDataset(
        lake=None,  # type: ignore[arg-type]
        universe=["A.US", "B.US"],
        start=date(2025, 1, 1),
        end=date(2025, 12, 31),
        train_ratio=0.5,
    )


def test_drift_fails_when_every_feature_extraction_errors():
    report = DriftTest(sample_dates=5).run(_Crashing({}), _dataset())
    assert report.passed is False
    # 5 train + 5 val dates x 2 tickers
    assert report.metrics["errors"] == 20.0
    assert report.metrics["attempts"] == 20.0
    assert "exploded" in report.notes


def test_drift_still_skips_when_strategy_has_no_features_and_no_errors():
    report = DriftTest(sample_dates=5).run(_Featureless({}), _dataset())
    assert report.passed is True
    assert report.metrics["errors"] == 0.0


def test_drift_reports_partial_error_count():
    report = DriftTest(max_psi=1e9, sample_dates=10).run(_SometimesCrashing({}), _dataset())
    assert report.passed is True
    assert report.metrics["errors"] == 10.0  # every 4th of 40 calls
    assert report.metrics["attempts"] == 40.0


# ---- RS-11: NaN features ---------------------------------------------------------


def test_psi_ignores_nan_at_any_position():
    import math

    import pytest

    from stonks.lab.survival.drift import _psi

    rng = __import__("random").Random(1)
    train = [rng.gauss(0, 1) for _ in range(200)]
    val = [rng.gauss(0.5, 1) for _ in range(200)]
    base = _psi(train, val, bins=10)
    assert base > 0.05
    nan = math.nan
    assert _psi([nan, *train], val, bins=10) == pytest.approx(base)
    assert _psi([*train[:50], nan, nan, *train[50:], nan], val, bins=10) == pytest.approx(base)
    assert _psi(train, [nan, *val, nan], bins=10) == pytest.approx(base)
    same = list(train)
    assert _psi([*same, nan, nan, nan], same, bins=10) == pytest.approx(0.0)


class _HalfNan(_Crashing):
    id = "half_nan_fake"

    def extract_features(self, ticker, as_of, lake):
        return Features(values={"x": float("nan") if ticker == "A.US" else 1.0 + as_of.day})


def test_drift_reports_the_nan_share():
    report = DriftTest(max_psi=1e9, sample_dates=10).run(_HalfNan({}), _dataset())
    assert report.metrics["nan_share"] == 0.5

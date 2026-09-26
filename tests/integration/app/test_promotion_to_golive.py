"""End to end (W2.3 / W2.6): a lab run on the ``promotion`` preset registers
the strategy with its survival reports, and the go-live check reads the
Monte Carlo band (``mc_trades`` ``p95_max_dd``) from them."""

from __future__ import annotations

import math
from datetime import date

import pandas as pd
import pytest

from stonks.app.golive import GoLiveService
from stonks.app.lab import LabRunRequest, execute_lab_run
from stonks.app.strategies import StrategyRef
from stonks.lab.survival.registry import resolve_preset
from stonks.registry.store import StrategyRegistry
from stonks.store.lake import DuckDBLake
from stonks.store.state import SqliteState
from stonks.strategies.examples.momentum import Momentum
from tests.paper_seed import TICK_ID, days, seed_shadow

MOMENTUM = f"{Momentum.__module__}:{Momentum.__name__}"
SERIAL = {"max_workers": 1}


class ShortMomentum(Momentum):
    """Momentum on a 5-bar lookback labels about 5 bars ahead, not the
    default month, so the walk-forward embargo stays short."""

    label_horizon_bars = 5


def _seed_waves(path) -> None:
    """Two oscillating tickers so a short-lookback momentum trades often."""
    dates = pd.bdate_range(start="2025-10-01", end="2026-04-01")
    rows = []
    for ticker, period in (("WAVE.US", 16), ("SWING.US", 20)):
        for i, d in enumerate(dates):
            c = 100.0 + 0.05 * i + 8.0 * math.sin(2 * math.pi * i / period)
            rows.append(
                {
                    "ticker": ticker,
                    "date": d.date(),
                    "open": c,
                    "high": c + 0.5,
                    "low": c - 0.5,
                    "close": c,
                    "adj_close": c,
                    "volume": 1_000_000,
                }
            )
    lake = DuckDBLake(path)
    try:
        lake.upsert_prices(pd.DataFrame(rows))
    finally:
        lake.close()


@pytest.fixture
def promoted(settings, seeded):
    _seed_waves(settings.lake.path)
    settings.lab.parallel = settings.lab.parallel.model_copy(update={"max_workers": 1})
    request = LabRunRequest(
        strategy=StrategyRef(class_path=MOMENTUM),
        universe=["WAVE.US", "SWING.US"],
        start=date(2025, 10, 1),
        end=date(2026, 4, 1),
        tuner="grid",
        grid_size=2,
        budget=2,
        preset="promotion",
        register_strategy=True,
        walk_forward={"n_splits": 2, "max_workers": 1},
        test_options={
            "mcpt": {"n_permutations": 3, **SERIAL},
            "mc_trades": {"n_paths": 1000, "min_trades": 3},
            "cost_stress": SERIAL,
            "plateau": SERIAL,
            "cross_instrument": {"min_tickers": 2, **SERIAL},
        },
        hypothesis="short swings mean-revert slowly enough to ride them",
    )
    lake = DuckDBLake(settings.lake.path)
    state = SqliteState(settings.state.path)
    try:
        execution = execute_lab_run(
            settings,
            ShortMomentum,
            request,
            lake=lake,
            state=state,
            fixed_params={"lookback_days": 5, "skip_days": 0, "threshold": 0.0},
        )
    finally:
        lake.close()
        state.close()
    return execution


def test_promotion_run_registers_and_golive_shows_the_monte_carlo_band(settings, promoted):
    sid = promoted.registered_id
    assert sid is not None
    ids = [r.test_id for r in promoted.result.survival_reports]
    assert ids == resolve_preset("promotion")
    mc = next(r for r in promoted.result.survival_reports if r.test_id == "mc_trades")
    assert "p95_max_dd" in mc.metrics, mc.notes
    p95 = abs(mc.metrics["p95_max_dd"])

    with SqliteState(settings.state.path) as state:
        stored = StrategyRegistry(state=state, artifacts_dir=settings.registry.artifacts_dir)
        stored_ids = {r.test_id for r in stored.get_reports(sid)}
    assert stored_ids >= set(ids)

    with SqliteState(settings.state.path) as state:  # a flat paper period
        state.execute(
            "INSERT INTO tick_runs (id, started_at, finished_at, status) VALUES (?, ?, ?, 'ok')",
            [TICK_ID, "2026-01-01T00:00:00+00:00", "2026-01-01T00:01:00+00:00"],
        )
        seed_shadow(state, sid, [(d, 10_000.0) for d in days(30)], fills=6)

    report = GoLiveService(_context(settings)).check(sid)
    band = next(c for c in report.checks if c.name == "within_mc_band")
    assert band.limit == pytest.approx(p95)
    assert band.value == pytest.approx(0.0)  # the flat paper period
    assert band.passed and "Monte Carlo p95" in band.detail
    preset = next(c for c in report.checks if c.name == "promotion_preset")
    assert preset.passed, preset.detail
    assert next(c for c in report.checks if c.name == "hypothesis_recorded").passed
    assert report.checklist.n_trials_class is not None


def _context(settings):
    from stonks.app.context import AppContext

    return AppContext(settings)

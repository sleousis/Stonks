"""The pipeline backtest builds its daily history only when something reads it.

``single_winner`` with no risk policy reads no volatilities and no risk
context, so the engine must not slice and rebase the history on every
decision. The result is the same either way.
"""

from __future__ import annotations

from datetime import date

import pandas as pd
import pytest

from stonks.backtest.engine import BacktestConfig, Backtester
from stonks.backtest.simulated_broker import SimulatedBroker
from stonks.core.types import Portfolio
from stonks.store.lake import DuckDBLake
from stonks.strategies.examples.momentum import Momentum

UNIVERSE = ["UP.US", "FLAT.US", "DOWN.US"]


@pytest.fixture
def lake(tmp_path):
    lake = DuckDBLake(tmp_path / "lake.duckdb")
    lake.migrate()
    dates = pd.bdate_range("2025-10-01", "2026-03-20")
    n = len(dates)
    rows = []
    for ticker, closes in [
        ("UP.US", [100.0 + 100.0 * i / (n - 1) for i in range(n)]),
        ("FLAT.US", [50.0] * n),
        ("DOWN.US", [100.0 - 40.0 * i / (n - 1) for i in range(n)]),
    ]:
        for d, c in zip(dates, closes, strict=True):
            rows.append(
                {
                    "ticker": ticker,
                    "date": d.date(),
                    "open": c,
                    "high": c + 0.5,
                    "low": c - 0.5,
                    "close": c,
                    "adj_close": c,
                    "volume": 1e6,
                }
            )
    lake.upsert_prices(pd.DataFrame(rows))
    yield lake
    lake.close()


def _run(lake, **config) -> tuple[Backtester, float]:
    broker = SimulatedBroker(Portfolio(cash=10_000.0))
    engine = Backtester(
        [Momentum({"lookback_days": 5, "threshold": 0.0, "allocation": 0.5})],
        broker,
        lake,
        BacktestConfig(
            start=date(2026, 1, 5),
            end=date(2026, 3, 20),
            universe=UNIVERSE,
            construction="single_winner",
            **config,
        ),
    )
    return engine, engine.run().equity_curve[-1]


def test_single_winner_without_risk_never_slices_history(lake, monkeypatch):
    calls: list[object] = []
    real = Backtester._history_until

    def spy(self, as_of):
        calls.append(as_of)
        return real(self, as_of)

    monkeypatch.setattr(Backtester, "_history_until", spy)
    _, lazy = _run(lake)
    assert calls == []

    # forcing the history on gives the same result
    monkeypatch.setattr(Backtester, "_needs_history", lambda self: True)
    _, eager = _run(lake)
    assert calls  # the forced run did slice it
    assert lazy == pytest.approx(eager, rel=0, abs=1e-9)
    assert lazy != 10_000.0  # it traded


def test_a_risk_policy_still_reads_history(lake):
    from stonks.config import RiskPolicy

    engine, _ = _run(lake, risk=RiskPolicy())
    assert engine._needs_history()


def test_a_vol_constructor_still_reads_history(lake):
    engine, _ = _run(lake)
    assert not engine._needs_history()
    engine = Backtester(
        [],
        SimulatedBroker(Portfolio(cash=1.0)),
        lake,
        BacktestConfig(
            start=date(2026, 1, 5),
            end=date(2026, 1, 9),
            universe=UNIVERSE,
            construction="inverse_vol",
        ),
    )
    assert engine._needs_history()

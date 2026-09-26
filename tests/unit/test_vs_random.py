"""Versus-random test (BL-35, after Woodriff)."""

from __future__ import annotations

from types import SimpleNamespace

import numpy as np
import pandas as pd
import pytest

from stonks.lab.objectives import SharpeObjective
from stonks.lab.parallel import ParallelSettings
from stonks.lab.survival.base import TuningSetup
from stonks.lab.survival.registry import build_survival_test, survival_test_names
from stonks.lab.survival.vs_random import bootstrap_noise_bars
from stonks.lab.tuning.grid import GridTuner
from tests.fixtures.signal_research import WeekdaySignal, dataset_for, signal_lake

TICKERS = ["WA", "WB"]


def _setup() -> TuningSetup:
    return TuningSetup(
        tuner=GridTuner(grid_size=5, parallel=ParallelSettings(max_workers=1)),
        objective=SharpeObjective(),
        budget=5,
    )


def _test(**options):
    test = build_survival_test("vs_random", {"k": 10, "max_workers": 1, **options})
    test.bind_tuning(_setup())
    return test


@pytest.fixture(scope="module")
def edge_lake():
    lake = signal_lake(
        ":memory:",
        TICKERS,
        periods=240,
        seed=21,
        weekday_edge={"WA": (2, 0.012), "WB": (2, 0.012)},
    )
    yield lake
    lake.close()


@pytest.fixture(scope="module")
def plain_lake():
    lake = signal_lake(":memory:", TICKERS, periods=240, seed=22)
    yield lake
    lake.close()


def test_registered():
    assert "vs_random" in survival_test_names()


def test_planted_edge_beats_the_noise(edge_lake):
    report = _test().run(WeekdaySignal({"weekday": 2}), dataset_for(edge_lake, TICKERS))
    assert report.passed, report.notes
    m = report.metrics
    assert m["real_best"] > m["noise_best_q"]
    assert m["real_oos"] > m["noise_oos_median"]
    assert m["k"] == 10
    assert m["p_value"] == pytest.approx(1 / 11)


def test_pure_noise_strategy_fails(plain_lake):
    report = _test().run(WeekdaySignal({"weekday": 0}), dataset_for(plain_lake, TICKERS))
    assert not report.passed


def test_bind_run_takes_the_setup_from_the_run(edge_lake):
    test = build_survival_test("vs_random", {"k": 5, "max_workers": 1})
    test.bind_run(SimpleNamespace(setup=_setup()))
    report = test.run(WeekdaySignal({"weekday": 2}), dataset_for(edge_lake, TICKERS))
    assert report.metrics["k"] == 5


def test_without_a_tuning_setup_fails_with_a_note(edge_lake):
    test = build_survival_test("vs_random", {"k": 5, "max_workers": 1})
    report = test.run(WeekdaySignal({}), dataset_for(edge_lake, TICKERS))
    assert not report.passed
    assert "tuning setup" in report.notes


def test_rejects_bad_options():
    with pytest.raises(ValueError):
        build_survival_test("vs_random", {"k": 1})
    with pytest.raises(ValueError):
        build_survival_test("vs_random", {"quantile": 1.0})
    with pytest.raises(ValueError):
        build_survival_test("vs_random", {"block": 0})


def _history(n: int = 60, n_before: int = 10, seed: int = 0):
    rng = np.random.default_rng(seed)
    ts = pd.bdate_range("2024-01-01", periods=n)
    close = 100 * np.exp(np.cumsum(rng.normal(0, 0.01, n)))
    opens = close * np.exp(rng.normal(0, 0.003, n))
    frame = pd.DataFrame(
        {
            "ticker": "X",
            "timestamp": ts,
            "open": opens,
            "high": np.maximum(opens, close) * 1.01,
            "low": np.minimum(opens, close) * 0.99,
            "close": close,
            "adj_close": close,
            "volume": 1.0,
        }
    )
    return {"X": (frame, n_before)}, ts


def test_noise_bars_keep_history_and_resample_train_bars_only():
    history, ts = _history()
    train_end = ts[39].date()
    out = bootstrap_noise_bars(history, train_end, block=5, seed=3)["X"]
    real = history["X"][0]
    assert len(out) == len(real)
    assert (out["timestamp"] == real["timestamp"]).all()
    # history before the train window is untouched
    pd.testing.assert_frame_equal(out.iloc[:10], real.iloc[:10])
    # every rebuilt body comes from a train-window bar (rows 10..39)
    body = np.log(out["close"] / out["open"]).to_numpy()[10:]
    pool = np.log(real["close"] / real["open"]).to_numpy()[10:40]
    assert np.all(np.min(np.abs(body[:, None] - pool[None, :]), axis=1) < 1e-9)
    assert not np.allclose(out["close"].to_numpy()[10:], real["close"].to_numpy()[10:])
    assert (out["high"] >= out[["open", "close"]].max(axis=1) - 1e-9).all()


def test_noise_bars_are_seeded():
    history, ts = _history()
    a = bootstrap_noise_bars(history, ts[39].date(), block=5, seed=3)["X"]
    b = bootstrap_noise_bars(history, ts[39].date(), block=5, seed=3)["X"]
    c = bootstrap_noise_bars(history, ts[39].date(), block=5, seed=4)["X"]
    pd.testing.assert_frame_equal(a, b)
    assert not a["close"].equals(c["close"])


def test_noise_bars_share_one_draw_across_tickers():
    history, ts = _history(seed=1)
    one, n_before = history["X"]
    other = one.assign(
        ticker="Y",
        open=one["open"] * 2,
        high=one["high"] * 2,
        low=one["low"] * 2,
        close=one["close"] * 2,
        adj_close=one["close"] * 2,
    )
    out = bootstrap_noise_bars(
        {"X": (one, n_before), "Y": (other, n_before)}, ts[39].date(), block=5, seed=9
    )
    np.testing.assert_allclose(out["Y"]["close"].to_numpy(), 2 * out["X"]["close"].to_numpy())

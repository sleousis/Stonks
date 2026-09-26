"""Unit tests for the RSI-PCA reference strategy (ML lifecycle: fit, predict,
save/load).
"""

from __future__ import annotations

import json
from datetime import date
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from stonks.core.interval import Interval
from stonks.core.types import Portfolio
from stonks.lab.dataset import LabDataset
from stonks.store.lake import DuckDBLake
from stonks.strategies.examples.rsi_pca import RSIPCAStrategy


@pytest.fixture
def lake_500d(tmp_path):
    """Lake with ~500 daily bars of a mildly trending random-walk series —
    enough for PCA + linear fit on a 70% training window."""
    rng = np.random.default_rng(11)
    dates = pd.bdate_range(start="2024-01-02", periods=500)
    log_close = np.log(100.0) + np.cumsum(rng.normal(0.0005, 0.015, len(dates)))
    close = np.exp(log_close)

    lake = DuckDBLake(tmp_path / "lake.duckdb")
    lake.migrate()
    lake.upsert_prices(
        pd.DataFrame(
            [
                {
                    "ticker": "X.US",
                    "date": d.date(),
                    "open": c,
                    "high": c + 0.3,
                    "low": c - 0.3,
                    "close": c,
                    "adj_close": c,
                    "volume": 1_000_000,
                }
                for d, c in zip(dates, close, strict=False)
            ]
        )
    )
    yield lake, dates
    lake.close()


def _dataset(lake, dates):
    return LabDataset(
        lake=lake,
        universe=["X.US"],
        start=dates[0].date(),
        end=dates[-1].date(),
        interval=Interval.DAY_1,
        train_ratio=0.7,
    )


# ---- shape / param spec ---------------------------------------------------


def test_parameter_spec_contains_expected_names():
    names = {s.name for s in RSIPCAStrategy.parameter_spec()}
    assert {
        "n_components",
        "lookahead",
        "long_quantile",
        "short_quantile",
        "rsi_period_min",
        "rsi_period_max",
        "interval",
        "ticker",
        "allocation",
    } <= names


def test_tunable_and_non_tunable_partition():
    specs = {s.name: s for s in RSIPCAStrategy.parameter_spec()}
    assert specs["n_components"].tunable is True
    assert specs["lookahead"].tunable is True
    assert specs["long_quantile"].tunable is True
    assert specs["short_quantile"].tunable is True
    assert specs["ticker"].tunable is False
    assert specs["rsi_period_min"].tunable is False
    assert specs["rsi_period_max"].tunable is False
    assert specs["interval"].tunable is False
    assert specs["allocation"].tunable is False


# ---- fit lifecycle --------------------------------------------------------


def test_is_fitted_false_before_fit():
    s = RSIPCAStrategy({"ticker": "X.US"})
    assert s.is_fitted is False


def test_fit_populates_state_and_returns_none(lake_500d):
    lake, dates = lake_500d
    s = RSIPCAStrategy({"ticker": "X.US", "n_components": 3, "lookahead": 5})
    out = s.fit(_dataset(lake, dates))
    assert out is None
    assert s.is_fitted is True
    assert s._rsi_means is not None
    assert s._evecs is not None
    assert s._coefs is not None
    assert s._long_thresh is not None
    assert s._short_thresh is not None
    # evecs shape: (n_rsi_periods, n_components)
    assert s._evecs.shape[1] == 3


def test_fit_raises_on_empty_training_data(tmp_path):
    empty = DuckDBLake(tmp_path / "empty.duckdb")
    empty.migrate()
    try:
        dataset = LabDataset(
            lake=empty,
            universe=["Y.US"],
            start=date(2024, 1, 1),
            end=date(2024, 12, 31),
            interval=Interval.DAY_1,
            train_ratio=0.7,
        )
        s = RSIPCAStrategy({"ticker": "Y.US"})
        with pytest.raises(ValueError, match="no bars"):
            s.fit(dataset)
    finally:
        empty.close()


# ---- estimate_return / decide --------------------------------------------


def test_estimate_return_is_none_before_fit():
    s = RSIPCAStrategy({"ticker": "X.US"})
    assert s.estimate_return("X.US", date(2025, 1, 1), lake=None) is None


def test_estimate_return_is_none_for_other_ticker_after_fit(lake_500d):
    lake, dates = lake_500d
    s = RSIPCAStrategy({"ticker": "X.US", "n_components": 2, "lookahead": 5})
    s.fit(_dataset(lake, dates))
    assert s.estimate_return("OTHER.US", dates[-1].date(), lake) is None


def test_estimate_return_returns_float_or_none_after_fit(lake_500d):
    lake, dates = lake_500d
    s = RSIPCAStrategy({"ticker": "X.US", "n_components": 3, "lookahead": 5})
    s.fit(_dataset(lake, dates))
    # Sample several dates in the validation window. At least one should
    # either exceed the long threshold (float return) or fall below it (None).
    val_dates = [dates[-i].date() for i in range(1, 40)]
    results = [s.estimate_return("X.US", d, lake) for d in val_dates]
    # All results are either None or floats; nothing crashes
    assert all(r is None or isinstance(r, float) for r in results)


def test_decide_buys_on_long_signal_when_flat(lake_500d):
    lake, dates = lake_500d
    s = RSIPCAStrategy({"ticker": "X.US", "n_components": 2, "lookahead": 5})
    s.fit(_dataset(lake, dates))
    portfolio = Portfolio(cash=10_000.0, positions={})
    orders = s.decide(
        my_picks=[(0.01, "X.US")],
        portfolio=portfolio,
        prices={"X.US": 100.0},
        as_of=dates[-1].date(),
    )
    assert len(orders) == 1
    assert orders[0].side == "buy"


def test_decide_sells_when_signal_off_and_holding(lake_500d):
    lake, dates = lake_500d
    s = RSIPCAStrategy({"ticker": "X.US", "n_components": 2, "lookahead": 5})
    s.fit(_dataset(lake, dates))
    portfolio = Portfolio(cash=0.0, positions={"X.US": 5.0})
    orders = s.decide(
        my_picks=[],
        portfolio=portfolio,
        prices={"X.US": 100.0},
        as_of=dates[-1].date(),
    )
    sells = [o for o in orders if o.side == "sell"]
    assert any(o.ticker == "X.US" for o in sells)


# ---- features -------------------------------------------------------------


def test_extract_features_after_fit_includes_pred_and_thresholds(lake_500d):
    lake, dates = lake_500d
    s = RSIPCAStrategy({"ticker": "X.US", "n_components": 2, "lookahead": 5})
    s.fit(_dataset(lake, dates))
    features = s.extract_features("X.US", dates[-1].date(), lake)
    assert "pred" in features.values
    assert "long_thresh" in features.values
    assert "short_thresh" in features.values
    assert "signal" in features.values


def test_extract_features_empty_before_fit():
    s = RSIPCAStrategy({"ticker": "X.US"})
    features = s.extract_features("X.US", date(2025, 1, 1), lake=None)
    assert features.values == {}


# ---- save / load lifecycle -----------------------------------------------


def test_save_writes_fitted_state(lake_500d, tmp_path):
    lake, dates = lake_500d
    s = RSIPCAStrategy({"ticker": "X.US", "n_components": 2, "lookahead": 5})
    s.fit(_dataset(lake, dates))

    artifact_path = tmp_path / "strategy_a"
    s.save(artifact_path)

    assert (artifact_path / "params.json").exists()
    assert (artifact_path / "meta.json").exists()
    assert (artifact_path / "fitted_state.json").exists()

    state = json.loads((artifact_path / "fitted_state.json").read_text())
    assert "rsi_means" in state
    assert "evecs" in state
    assert "coefs" in state
    assert "long_thresh" in state
    assert "short_thresh" in state


def test_load_restores_fitted_state(lake_500d, tmp_path):
    lake, dates = lake_500d
    s = RSIPCAStrategy({"ticker": "X.US", "n_components": 2, "lookahead": 5})
    s.fit(_dataset(lake, dates))
    as_of = dates[-1].date()
    pred_before = s._predict_current("X.US", as_of, lake)

    artifact_path = tmp_path / "strategy_b"
    s.save(artifact_path)

    loaded = RSIPCAStrategy.load(artifact_path)
    assert loaded.is_fitted is True
    pred_after = loaded._predict_current("X.US", as_of, lake)

    # bitwise-identical reconstruction may differ slightly due to JSON rounding,
    # but not by much
    if pred_before is None:
        assert pred_after is None
    else:
        assert abs(pred_before - pred_after) < 1e-6


def test_load_without_fitted_state_returns_unfitted_instance(tmp_path: Path):
    s = RSIPCAStrategy({"ticker": "X.US", "n_components": 2})
    s.save(tmp_path)
    # strip fitted_state if it somehow got written (it shouldn't, not fitted)
    state_path = tmp_path / "fitted_state.json"
    if state_path.exists():
        state_path.unlink()
    loaded = RSIPCAStrategy.load(tmp_path)
    assert loaded.is_fitted is False


# ---- holding period (hold_bars) --------------------------------------------


def _fitted(lake, dates, **params):
    s = RSIPCAStrategy({"ticker": "X.US", "n_components": 3, "long_quantile": 0.9, **params})
    s.fit(_dataset(lake, dates))
    return s


def _raw_long(s, lake, as_of):
    pred = s._predict_current("X.US", as_of, lake)
    return pred is not None and pred > s._long_thresh


def test_hold_bars_spec_is_tunable_int_1_to_48():
    spec = {s.name: s for s in RSIPCAStrategy.parameter_spec()}["hold_bars"]
    assert spec.kind == "int"
    assert spec.bounds == (1, 48)
    assert spec.tunable is True


def test_hold_bars_defaults_to_lookahead():
    assert RSIPCAStrategy({"lookahead": 11}).params["hold_bars"] == 11
    assert RSIPCAStrategy({}).params["hold_bars"] == RSIPCAStrategy({}).params["lookahead"]
    assert RSIPCAStrategy({"lookahead": 11, "hold_bars": 2}).params["hold_bars"] == 2


def test_artifacts_saved_before_hold_bars_load_with_the_old_exit_rule(tmp_path):
    s = RSIPCAStrategy({"ticker": "X.US", "lookahead": 9, "hold_bars": 4})
    s.save(tmp_path)
    assert RSIPCAStrategy.load(tmp_path).params["hold_bars"] == 4
    params = json.loads((tmp_path / "params.json").read_text())
    del params["hold_bars"]  # what an artifact saved before this param looks like
    (tmp_path / "params.json").write_text(json.dumps(params))
    # a registered strategy keeps the behavior its survival reports were made with
    assert RSIPCAStrategy.load(tmp_path).params["hold_bars"] == 1


def test_hold_bars_one_exits_on_first_bar_below_threshold(lake_500d):
    lake, dates = lake_500d
    s = _fitted(lake, dates, hold_bars=1)
    for d in dates[-60:]:
        pred = s._predict_current("X.US", d.date(), lake)
        expected = pred if pred is not None and pred > s._long_thresh else None
        assert s.estimate_return("X.US", d.date(), lake) == expected


def test_hold_bars_keeps_a_long_for_hold_bars_bars_after_each_signal(lake_500d):
    lake, dates = lake_500d
    k = 4
    s = _fitted(lake, dates, lookahead=5, hold_bars=k)
    val = list(dates[-120:])
    raw = [_raw_long(s, lake, d.date()) for d in val]
    held_without_signal = 0
    for i in range(k, len(val)):
        expected = any(raw[i - k + 1 : i + 1])
        got = s.estimate_return("X.US", val[i].date(), lake)
        assert (got is not None) == expected, val[i]
        if expected and not raw[i]:
            held_without_signal += 1
    assert held_without_signal > 0  # the hold actually extends positions


def test_hold_bars_replays_causally_so_a_fresh_instance_agrees(lake_500d, tmp_path):
    lake, dates = lake_500d
    walked = _fitted(lake, dates, lookahead=5, hold_bars=6)
    walked_out = {d.date(): walked.estimate_return("X.US", d.date(), lake) for d in dates[-80:]}

    walked.save(tmp_path / "a")
    for d in list(dates[-80:])[::7]:
        fresh = RSIPCAStrategy.load(tmp_path / "a")  # no history of earlier calls
        got = fresh.estimate_return("X.US", d.date(), lake)
        want = walked_out[d.date()]
        assert (got is None) == (want is None)
        if want is not None:
            assert got == pytest.approx(want, rel=1e-9)

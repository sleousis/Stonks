"""LearningRanker (roadmap 23.12): a gradient-boosting ranker trained on the
factor dataset, fitted on training windows only, holding the top slice."""

from __future__ import annotations

from datetime import date, datetime
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from stonks.core.interval import Interval
from stonks.core.types import Portfolio
from stonks.lab.catalog import strategy_catalog
from stonks.lab.dataset import LabDataset
from stonks.store.lake import DuckDBLake
from stonks.store.pit import PitSession
from stonks.strategies._common import decision_interval
from stonks.strategies.base import strategy_metadata
from stonks.strategies.examples.learning_ranker import LearningRanker

DATES = pd.bdate_range("2023-01-02", "2024-12-31")
UNIVERSE = [f"R{i:02d}.US" for i in range(12)]
TRAIN_END = date(2024, 6, 28)
D = datetime(2024, 9, 30)  # a month-end decision after the train window
#: Momentum and volatility as formulas: fast, and momentum carries the signal.
FACTORS = "Ref($close, 20)/$close, Std($close/Ref($close, 1)-1, 20)"


def _closes(seed: int = 3) -> dict[str, np.ndarray]:
    """Ticker ``i`` drifts by ``i``: past winners keep winning, so the 20-bar
    return ranks next month's return."""
    rng = np.random.default_rng(seed)
    out = {}
    for i, ticker in enumerate(UNIVERSE):
        rets = rng.normal(0.0008 * (i - 5.5), 0.01, len(DATES))
        out[ticker] = 50.0 * np.exp(np.cumsum(rets))
    return out


def _frame(ticker: str, closes: np.ndarray, dates=DATES) -> pd.DataFrame:
    return pd.DataFrame(
        {
            "ticker": ticker,
            "date": [d.date() for d in dates[: len(closes)]],
            "open": closes,
            "high": closes * 1.01,
            "low": closes * 0.99,
            "close": closes,
            "adj_close": closes,
            "volume": 1_000_000.0,
        }
    )


def _lake(closes: dict[str, np.ndarray], path: Path | None = None, dates=DATES) -> DuckDBLake:
    lake = DuckDBLake(path or Path(":memory:"))
    lake.migrate()
    lake.upsert_prices(
        pd.concat([_frame(t, c, dates) for t, c in closes.items()], ignore_index=True)
    )
    for ticker in closes:
        lake.con.execute("INSERT INTO instruments (id, asset_class) VALUES (?, 'equity')", [ticker])
    return lake


LAST_DAY = DATES[-1].date()


def _dataset(lake, universe=UNIVERSE, end=LAST_DAY, train_end=TRAIN_END) -> LabDataset:
    return LabDataset(
        lake=lake,
        universe=list(universe),
        start=DATES[0].date(),
        end=end,
        interval=Interval.DAY_1,
        train_end=train_end,
    )


def _ranker(**params) -> LearningRanker:
    base = {
        "factors": FACTORS,
        "horizon_bars": 10,
        "sample_step": 5,
        "max_iter": 60,
        "min_samples_leaf": 20,
        "top_pct": 0.25,
    }
    return LearningRanker(base | params)


@pytest.fixture(scope="module")
def lake():
    db = _lake(_closes())
    yield db
    db.close()


@pytest.fixture(scope="module")
def fitted(lake):
    strategy = _ranker()
    strategy.fit(_dataset(lake))
    return strategy


def _held(strategy, lake, as_of=D) -> dict[str, float]:
    view = PitSession(lake).at(as_of, decision_interval=Interval.DAY_1)
    with decision_interval(Interval.DAY_1):
        got = {t: strategy.estimate_return(t, as_of, view) for t in UNIVERSE}
    return {t: v for t, v in got.items() if v is not None}


# ---- catalog and metadata --------------------------------------------------------


def test_catalogued_retrainable_with_metadata():
    assert strategy_catalog()["learning_ranker"] is LearningRanker
    strategy = _ranker()
    assert strategy.retrainable
    meta = strategy_metadata(strategy)
    assert meta.alpha_family == "data_driven"
    assert strategy.label_horizon_bars == 11  # the label reads O[t+1+h]
    assert strategy.required_history_bars == 21  # the longest lookback plus one
    default = LearningRanker({})
    assert default.feature_names[0] == "KMID" and len(default.feature_names) == 157
    assert default.required_history_bars == LearningRanker.required_history_bars
    assert default.label_horizon_bars == LearningRanker.label_horizon_bars


def test_bad_params_are_refused():
    with pytest.raises(ValueError, match="factors"):
        LearningRanker({"factors": "Ref($close, -1)"})
    with pytest.raises(ValueError, match="top_pct"):
        _ranker(top_pct=0.0)
    with pytest.raises(ValueError):
        _ranker(model="xgboost")


def test_fundamentals_features_limit_the_ranker_to_equities():
    assert LearningRanker({"factors": "piotroski_f"}).applicable_asset_classes == ("equity",)


def test_unfitted_holds_nothing(lake):
    strategy = _ranker()
    assert not strategy.is_fitted
    assert _held(strategy, lake) == {}
    assert strategy.extract_features("R00.US", D, lake).values == {}
    assert strategy.ranker_report() is None


# ---- fitting ---------------------------------------------------------------------


def test_fit_learns_the_planted_signal_out_of_fold(fitted):
    state = fitted.fitted_state()
    assert state["feature_names"] == fitted.feature_names
    assert state["n_rows"] > 500 and state["n_segments"] == 1
    assert state["cv_folds"] == 3
    assert state["ic_mean"] > 0.3
    momentum, vol = fitted.feature_names
    assert state["importance"][momentum] > state["importance"][vol]
    assert state["train_end"] <= TRAIN_END.isoformat()
    assert state["last_label_end"] <= TRAIN_END.isoformat()


def test_held_slice_is_the_predicted_top(fitted, lake):
    held = _held(fitted, lake)
    assert len(held) == 3  # 25% of 12
    # the strongest drifts win
    assert set(held) <= {f"R{i:02d}.US" for i in range(7, 12)}
    assert all(0 < v <= 1 for v in held.values())
    features = fitted.extract_features(next(iter(held)), D, lake).values
    assert features["held"] == 1.0


def test_fit_never_reads_past_the_train_window(lake):
    """A lake whose bars after the train window are wild fits the same model
    as one that ends on the last train day."""
    closes = _closes()
    cut = int(np.searchsorted(DATES, pd.Timestamp(TRAIN_END), side="right"))
    truncated = _lake({t: c[:cut] for t, c in closes.items()})
    wild = {t: c.copy() for t, c in closes.items()}
    for i, c in enumerate(wild.values()):
        c[cut:] *= 5.0 if i % 2 else 0.1
    planted = _lake(wild)
    a, b = _ranker(), _ranker()
    a.fit(_dataset(truncated, end=TRAIN_END, train_end=TRAIN_END - pd.Timedelta(days=30)))
    # same train window, longer lake
    b.fit(
        LabDataset(
            lake=planted,
            universe=UNIVERSE,
            start=DATES[0].date(),
            end=TRAIN_END,
            interval=Interval.DAY_1,
            train_end=TRAIN_END - pd.Timedelta(days=30),
        )
    )
    assert a.fitted_state() == b.fitted_state()
    truncated.close()
    planted.close()


def test_cv_segments_are_fitted_apart_and_ignore_the_test_block(lake):
    """Each purged segment builds its own labels: bars in the test block
    between the segments (beyond the next segment's warm-up) change nothing."""
    seg1 = (DATES[0].date(), date(2023, 8, 31))
    seg2 = (date(2024, 1, 2), TRAIN_END)
    closes = _closes()
    altered = {t: c.copy() for t, c in closes.items()}
    lo = int(np.searchsorted(DATES, pd.Timestamp(seg1[1]), side="right"))
    for c in altered.values():
        c[lo : lo + 30] *= 3.0  # early in the test block, far before seg2's warm-up
    other = _lake(altered)
    results = []
    for db in (lake, other):
        strategy = _ranker()
        strategy.fit(_dataset(db).with_train_segments([seg1, seg2]))
        results.append(strategy.fitted_state())
    assert results[0]["n_segments"] == 2
    assert results[0] == results[1]
    other.close()


def test_fit_is_deterministic_for_a_seed(lake, fitted):
    again = _ranker()
    again.fit(_dataset(lake))
    assert again.fitted_state() == fitted.fitted_state()
    assert _held(again, lake) == _held(fitted, lake)


def test_fit_refuses_intraday_and_a_single_name(lake):
    with pytest.raises(ValueError, match="daily"):
        _ranker().fit(
            LabDataset(
                lake=lake,
                universe=UNIVERSE,
                start=DATES[0].date(),
                end=DATES[-1].date(),
                interval=Interval.HOUR_1,
                train_end=TRAIN_END,
            )
        )
    with pytest.raises(ValueError, match="labelled rows"):
        _ranker().fit(_dataset(lake, universe=["R00.US"]))


def test_diagnostic_can_be_turned_off(lake):
    strategy = _ranker(cv_folds=0)
    strategy.fit(_dataset(lake))
    state = strategy.fitted_state()
    assert state["cv_folds"] == 0 and state["importance"] == {}
    assert _held(strategy, lake)


# ---- scoring ---------------------------------------------------------------------


def test_scores_hold_between_month_ends(fitted, lake):
    mid = datetime(2024, 10, 15)
    assert fitted.anchor(mid) == date(2024, 9, 30)
    assert fitted.anchor(D) == date(2024, 9, 30)
    assert _held(fitted, lake, mid) == _held(fitted, lake, D)


def test_intraday_decision_on_a_month_end_uses_the_previous_anchor(fitted):
    with decision_interval(Interval.HOUR_1):
        assert fitted.anchor(datetime(2024, 9, 30, 15)) == date(2024, 8, 30)


def test_quarterly_rebalance_anchors_on_quarter_ends(lake):
    strategy = _ranker(rebalance_months="3,6,9,12")
    assert strategy.anchor(datetime(2024, 11, 20)) == date(2024, 9, 30)


def test_decide_rebalances_only_on_anchors(fitted, lake):
    held = _held(fitted, lake)
    picks = [(v, t) for t, v in held.items()]
    prices = dict.fromkeys(UNIVERSE, 50.0)
    portfolio = Portfolio(cash=100_000.0, positions={})
    orders = fitted.decide(picks, portfolio, prices, D)
    assert {o.ticker for o in orders} == set(held)
    assert fitted.decide(picks, portfolio, prices, datetime(2024, 10, 15)) == []


def test_blind_to_future_rows_behind_a_point_in_time_lake(fitted):
    """Future bars that would flip every signal change nothing at D, even
    when D's answer is asked about a later day by mistake."""
    closes = _closes()
    cut = int(np.searchsorted(DATES, pd.Timestamp(D), side="right"))
    past = _lake({t: c[:cut] for t, c in closes.items()})
    wild = {t: c.copy() for t, c in closes.items()}
    for i, c in enumerate(wild.values()):
        c[cut:] *= 0.1 if i >= 6 else 5.0
    planted = _lake(wild)
    later = DATES[cut:]
    planted.upsert_prices(_frame("NEW.US", np.full(len(later), 10.0), later))  # lists after D
    for asked in (D, datetime(2024, 12, 31)):
        view_a = PitSession(past).at(D, decision_interval=Interval.DAY_1)
        view_b = PitSession(planted).at(D, decision_interval=Interval.DAY_1)
        with decision_interval(Interval.DAY_1):
            a = {t: fitted.estimate_return(t, asked, view_a) for t in [*UNIVERSE, "NEW.US"]}
            b = {t: fitted.estimate_return(t, asked, view_b) for t in [*UNIVERSE, "NEW.US"]}
        assert a == b
    past.close()
    planted.close()


# ---- edge cases -------------------------------------------------------------------


def test_flat_prices_fit_and_score_finitely():
    flat = _lake({t: np.full(len(DATES), 20.0 + i) for i, t in enumerate(UNIVERSE)})
    strategy = _ranker()
    strategy.fit(_dataset(flat))
    held = _held(strategy, flat)
    assert len(held) == 3 and all(np.isfinite(v) for v in held.values())
    flat.close()


def test_nan_closes_and_a_delisted_name(lake):
    closes = _closes()
    closes["R03.US"][-60:-55] = np.nan
    delisted = {t: c for t, c in closes.items() if t != "R11.US"}
    db = _lake(delisted)
    cut = int(np.searchsorted(DATES, pd.Timestamp(date(2024, 3, 1))))
    db.upsert_prices(_frame("R11.US", closes["R11.US"][:cut]))  # stops trading in March
    strategy = _ranker()
    strategy.fit(_dataset(db))
    held = _held(strategy, db)
    assert "R11.US" not in held
    assert held and all(np.isfinite(v) for v in held.values())
    db.close()


def test_one_name_universe_at_scoring_time_is_held(fitted, lake):
    one = LearningRanker({**fitted.params, "universe": "R05.US"})
    one._model, one._state = fitted._model, fitted.fitted_state()
    assert _held(one, lake) == {"R05.US": 1.0}


# ---- persistence ------------------------------------------------------------------


def test_round_trips_through_save_and_load(fitted, lake, tmp_path):
    fitted.save(tmp_path / "bundle")
    loaded = LearningRanker.load(tmp_path / "bundle")
    assert loaded.fitted_state() == fitted.fitted_state()
    assert _held(loaded, lake) == _held(fitted, lake)


def test_load_refuses_a_model_trained_on_other_features(fitted, tmp_path):
    fitted.save(tmp_path / "bundle")
    params = tmp_path / "bundle" / "params.json"
    params.write_text(params.read_text().replace("Ref($close, 20)/$close", "Ref($close, 5)/$close"))
    with pytest.raises(ValueError, match="different features"):
        LearningRanker.load(tmp_path / "bundle")


def test_unfitted_saves_params_only(tmp_path):
    strategy = _ranker()
    strategy.save(tmp_path / "b")
    assert not LearningRanker.load(tmp_path / "b").is_fitted


def test_ranker_report_carries_importance_and_ic(fitted):
    report = fitted.ranker_report()
    assert report is not None
    assert report.model == "hist_gbm" and report.cv_folds == 3
    assert set(report.importance) == set(fitted.feature_names)
    assert report.ic_by_date and report.summary["ic_mean"] > 0

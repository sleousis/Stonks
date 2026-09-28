"""TrendlineMetaLabelStrategy with calibrated probabilities, conformal
abstention, the feature schema check and the training feature profile
(roadmap 23.10)."""

from __future__ import annotations

import json
import math

import numpy as np
import pytest

from stonks.features.calibration import (
    ConformalAbstainer,
    PlattCalibrator,
    ProbabilityPolicy,
)
from stonks.features.feature_profile import PROFILE_FILE, FeatureProfile, FeatureSchemaError
from stonks.strategies.examples.trendline_meta_label import (
    FEATURE_NAMES,
    TrendlineMetaLabelStrategy,
)
from tests.unit.nt888_helpers import make_lake, write_bars
from tests.unit.test_trendline_meta_label import (
    DATES,
    TRAIN_END_I,
    _closes,
    _dataset,
    _FixedProb,
    _open_trade_day,
    _strategy,
    _volumes,
)


@pytest.fixture
def lake(tmp_path):
    db = make_lake(tmp_path / "lake.duckdb")
    write_bars(db, "X.US", DATES, _closes(), _volumes())
    yield db
    db.close()


def _constant_policy(p: float, alpha_q: float | None = None) -> ProbabilityPolicy:
    cal = PlattCalibrator()
    cal.load_params({"a": 0.0, "b": math.log(p / (1 - p))})
    abstainer = None
    if alpha_q is not None:
        abstainer = ConformalAbstainer(0.1)
        abstainer.q = alpha_q
    return ProbabilityPolicy(cal, abstainer, n_fit=10)


def test_calibration_is_a_tunable_choice_and_conformal_is_off_by_default():
    spec = {s.name: s for s in TrendlineMetaLabelStrategy.parameter_spec()}
    assert spec["calibration"].tunable
    assert set(spec["calibration"].bounds) == {"none", "isotonic", "platt"}
    assert spec["calibration"].default == "none"
    assert spec["conformal_alpha"].default == 0.0


def test_fit_with_calibration_stores_a_policy_fitted_on_purged_oof(lake):
    s = _strategy(calibration="platt", conformal_alpha=0.2)
    s.fit(_dataset(lake))
    state = s.fitted_state()
    assert state["probability"]["calibrator"]["kind"] == "platt"
    assert state["probability"]["conformal"]["alpha"] == 0.2
    assert state["probability"]["n_fit"] <= state["n_trades"]


def test_calibration_needs_the_purged_folds(lake):
    with pytest.raises(ValueError, match="cv_folds"):
        _strategy(calibration="isotonic", cv_folds=0).fit(_dataset(lake))
    with pytest.raises(ValueError, match="cv_folds"):
        _strategy(conformal_alpha=0.2, cv_folds=1).fit(_dataset(lake))


def test_calibrated_probability_sets_the_gate(lake):
    s = _strategy(calibration="platt")
    s.fit(_dataset(lake))
    as_of = _open_trade_day(s, lake)
    s._classifier = _FixedProb(0.9)
    s._prob_memo.clear()
    s._policy = _constant_policy(0.3)  # the raw 0.9 is really 0.3
    assert s.estimate_return("X.US", as_of, lake) is None
    s._policy = _constant_policy(0.8)
    assert s.estimate_return("X.US", as_of, lake) is not None
    assert s.extract_features("X.US", as_of, lake).values["p_win"] == pytest.approx(0.8)


def test_conformal_abstention_blocks_an_ambiguous_trade(lake):
    s = _strategy(calibration="platt", conformal_alpha=0.1)
    s.fit(_dataset(lake))
    as_of = _open_trade_day(s, lake)
    s._classifier = _FixedProb(0.9)
    s._prob_memo.clear()
    s._policy = _constant_policy(0.8, alpha_q=0.95)  # both classes in the set
    assert s.estimate_return("X.US", as_of, lake) is None
    s._policy = _constant_policy(0.8, alpha_q=0.5)  # only class 1
    assert s.estimate_return("X.US", as_of, lake) is not None


def test_policy_fit_ignores_bars_after_train_end(tmp_path, lake):
    """Future shock: the calibration and the profile are the same when every
    bar after the train window changes."""
    s = _strategy(calibration="isotonic", conformal_alpha=0.2)
    s.fit(_dataset(lake))
    shocked_closes = _closes().copy()
    shocked_closes[TRAIN_END_I + 1 :] *= 3.0
    other = make_lake(tmp_path / "shocked.duckdb")
    write_bars(other, "X.US", DATES, shocked_closes, _volumes())
    try:
        t = _strategy(calibration="isotonic", conformal_alpha=0.2)
        t.fit(_dataset(other))
    finally:
        other.close()
    assert t.fitted_state()["probability"] == s.fitted_state()["probability"]
    assert t.feature_profile() == s.feature_profile()


def test_feature_profile_after_fit(lake):
    s = _strategy()
    assert s.feature_profile() is None
    s.fit(_dataset(lake))
    profile = s.feature_profile()
    assert isinstance(profile, FeatureProfile)
    assert profile.names == FEATURE_NAMES
    assert profile.n_train == s.fitted_state()["n_trades"]


def test_save_load_keeps_policy_and_profile(lake, tmp_path):
    s = _strategy(calibration="platt", conformal_alpha=0.3)
    s.fit(_dataset(lake))
    s.save(tmp_path / "art")
    assert (tmp_path / "art" / PROFILE_FILE).exists()
    loaded = TrendlineMetaLabelStrategy.load(tmp_path / "art")
    assert loaded.feature_profile() == s.feature_profile()
    assert loaded.fitted_state()["probability"] == s.fitted_state()["probability"]
    for i in range(560, 700, 5):
        as_of = DATES[i].to_pydatetime()
        assert loaded.estimate_return("X.US", as_of, lake) == s.estimate_return("X.US", as_of, lake)


def test_load_refuses_a_model_trained_on_other_features(lake, tmp_path):
    s = _strategy()
    s.fit(_dataset(lake))
    s.save(tmp_path / "art")
    state_file = tmp_path / "art" / "fitted_state.json"
    state = json.loads(state_file.read_text())
    state["feature_names"] = list(reversed(FEATURE_NAMES))
    state_file.write_text(json.dumps(state))
    with pytest.raises(FeatureSchemaError, match="order"):
        TrendlineMetaLabelStrategy.load(tmp_path / "art")


def test_load_refuses_a_profile_of_other_features(lake, tmp_path):
    s = _strategy()
    s.fit(_dataset(lake))
    s.save(tmp_path / "art")
    path = tmp_path / "art" / PROFILE_FILE
    payload = json.loads(path.read_text())
    payload["names"] = [*payload["names"][:-1], "renamed"]
    path.write_text(json.dumps(payload))
    with pytest.raises(FeatureSchemaError):
        TrendlineMetaLabelStrategy.load(tmp_path / "art")


def test_live_feature_rows_come_from_extract_features(lake):
    s = _strategy()
    s.fit(_dataset(lake))
    as_of = _open_trade_day(s, lake)
    row = s.model_feature_row("X.US", as_of, lake)
    assert row is not None and list(row) == list(FEATURE_NAMES)
    assert np.all(np.isfinite(list(row.values())))
    assert s.model_feature_row("OTHER.US", as_of, lake) is None


def test_feature_importance_runs_on_the_training_trades(lake):
    from stonks.lab.importance import feature_importance

    s = _strategy(n_estimators=20)
    data = s.training_set(_dataset(lake))
    assert data.feature_names == FEATURE_NAMES
    assert all(t <= np.datetime64(DATES[TRAIN_END_I]) for t in data.t1)
    report = feature_importance(s, _dataset(lake), folds=3, methods=("mda", "sfi"))
    assert {r.name for r in report.tables[0].rows} == set(FEATURE_NAMES)
    assert report.n_samples == len(data.y)


def test_lab_importance_command_writes_json_and_html(tmp_path):
    from stonks.lab.importance import main

    path = tmp_path / "cli.duckdb"
    db = make_lake(path)
    write_bars(db, "X.US", DATES, _closes(), _volumes())
    db.close()
    params = {"ticker": "X.US", "lookback": 24, "hold_period": 6, "atr_lookback": 50}
    params["n_estimators"] = 20
    argv = ["--strategy", "trendline_meta_label", "--params", json.dumps(params)]
    argv += ["--tickers", "X.US", "--start", DATES[0].date().isoformat()]
    argv += ["--end", DATES[-1].date().isoformat()]
    argv += ["--train-end", DATES[TRAIN_END_I].date().isoformat()]
    argv += ["--folds", "3", "--methods", "sfi", "--lake", str(path)]
    argv += ["--json", str(tmp_path / "imp.json"), "--html", str(tmp_path / "imp.html")]
    assert main(argv) == 0
    payload = json.loads((tmp_path / "imp.json").read_text())
    assert payload["tables"][0]["method"] == "sfi"
    assert "Feature importance" in (tmp_path / "imp.html").read_text(encoding="utf-8")

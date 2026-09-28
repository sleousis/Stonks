"""Roadmap 23.12: the learning ranker is versioned through the model lifecycle.
The retrain job refits it into a candidate version (a model book) and the
live version stays until a governed swap."""

from __future__ import annotations

from datetime import date, datetime

import pytest

from stonks.core.interval import Interval
from stonks.lifecycle.retrain import retrain_models
from stonks.lifecycle.settings import ModelLifecycleSettings
from stonks.registry.store import StrategyRegistry
from stonks.registry.versions import ModelVersionRegistry
from stonks.store.pit import PitSession
from stonks.store.state import SqliteState
from stonks.strategies._common import decision_interval
from stonks.strategies.examples.learning_ranker import LearningRanker
from tests.unit.test_learning_ranker import FACTORS, UNIVERSE, _closes, _lake

AS_OF = date(2024, 12, 31)


@pytest.fixture
def env(tmp_path):
    lake = _lake(_closes(), tmp_path / "lake.duckdb")
    state = SqliteState(tmp_path / "state.sqlite")
    state.migrate()
    registry = StrategyRegistry(state=state, artifacts_dir=tmp_path / "artifacts")
    yield lake, state, registry
    state.close()
    lake.close()


def test_retrain_makes_a_candidate_ranker_and_keeps_the_live_one(env):
    lake, state, registry = env
    params = {
        "factors": FACTORS,
        "horizon_bars": 10,
        "max_iter": 40,
        "min_samples_leaf": 20,
        "cv_folds": 0,
    }
    sid = registry.register(LearningRanker(params), reports=[])

    summary = retrain_models(
        state,
        lake,
        registry,
        ModelLifecycleSettings(lookback_days=500),
        as_of=AS_OF,
        universe=UNIVERSE,
        actor="test",
        max_workers=1,
    )

    assert [(o.strategy_id, o.status, o.version) for o in summary.outcomes] == [
        (sid, "candidate", 2)
    ]
    versions = ModelVersionRegistry.on(registry)
    assert {v.version: v.status for v in versions.list(sid)} == {1: "live", 2: "candidate"}
    candidate = versions.get(sid, 2)
    assert candidate.fit["feature_names"] and candidate.fit["n_rows"] > 0
    assert candidate.fit["last_label_end"] <= "2024-12-30"  # nothing past the fit's last day
    fitted = versions.load(sid, 2)
    assert isinstance(fitted, LearningRanker) and fitted.is_fitted
    view = PitSession(lake).at(datetime(2024, 12, 31), decision_interval=Interval.DAY_1)
    with decision_interval(Interval.DAY_1):
        held = [t for t in UNIVERSE if fitted.estimate_return(t, AS_OF, view) is not None]
    assert held
    # the live version is the registered, unfitted one until a swap
    assert not registry.load(sid).is_fitted  # type: ignore[attr-defined]

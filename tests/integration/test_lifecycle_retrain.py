"""Roadmap 22.6: scheduled retraining adds candidate versions, never swaps."""

from __future__ import annotations

from datetime import date

import pytest

from stonks.lifecycle.retrain import retrain_models
from stonks.lifecycle.settings import ModelLifecycleSettings
from stonks.registry.store import StrategyRegistry
from stonks.registry.versions import ModelVersionRegistry
from stonks.store.state import SqliteState
from stonks.strategies.examples.buy_and_hold import BuyAndHold
from tests.fixtures.governance import seed_status
from tests.fixtures.lifecycle import FailingFit, MeanFit

AS_OF = date(2026, 3, 20)
SETTINGS = ModelLifecycleSettings(lookback_days=60)


@pytest.fixture
def env(tmp_path, lake_trending):
    state = SqliteState(tmp_path / "state.sqlite")
    state.migrate()
    registry = StrategyRegistry(state=state, artifacts_dir=tmp_path / "artifacts")
    yield lake_trending, state, registry
    state.close()


def _run(env, **kwargs):
    lake, state, registry = env
    kwargs.setdefault("universe", ["UP.US"])
    return retrain_models(
        state, lake, registry, SETTINGS, as_of=AS_OF, actor="test", max_workers=1, **kwargs
    )


def test_retrain_adds_a_candidate_and_keeps_the_live_model(env):
    _, _, registry = env
    sid = registry.register(MeanFit({"ticker": "UP.US"}), reports=[])
    plain = registry.register(BuyAndHold({"ticker": "UP.US"}), reports=[])

    summary = _run(env)

    assert [(o.strategy_id, o.status, o.version) for o in summary.outcomes] == [
        (sid, "candidate", 2)
    ]
    versions = ModelVersionRegistry.on(registry)
    assert {v.version: v.status for v in versions.list(sid)} == {1: "live", 2: "candidate"}
    candidate = versions.get(sid, 2)
    assert candidate.train_end == AS_OF
    assert candidate.fit["train_window"][0] == "2026-01-19"
    assert candidate.artifact_path.is_dir()
    fitted = versions.load(sid, 2)
    assert isinstance(fitted, MeanFit) and fitted.mean_close is not None
    # the live model is untouched: the registered, unfitted one
    assert registry.load(sid).mean_close is None  # type: ignore[attr-defined]
    assert versions.list(plain)[0].status == "live"


def test_same_day_rerun_is_a_no_op_unless_forced(env):
    _, _, registry = env
    sid = registry.register(MeanFit({"ticker": "UP.US"}), reports=[])
    _run(env)
    again = _run(env)
    assert [(o.status, o.detail) for o in again.outcomes] == [
        ("skipped", "fitted up to 2026-03-20 already")
    ]
    forced = _run(env, force=True)
    assert [(o.status, o.version) for o in forced.outcomes] == [("candidate", 3)]
    statuses = {v.version: v.status for v in ModelVersionRegistry.on(registry).list(sid)}
    assert statuses == {1: "live", 2: "rejected", 3: "candidate"}


def test_failed_fit_is_recorded_and_others_still_fit(env):
    _, _, registry = env
    bad = registry.register(FailingFit({"ticker": "UP.US"}), reports=[])
    good = registry.register(MeanFit({"ticker": "UP.US"}), reports=[])
    summary = _run(env)
    by_sid = {o.strategy_id: o for o in summary.outcomes}
    assert by_sid[bad].status == "failed"
    assert "too few trades" in (by_sid[bad].detail or "")
    assert by_sid[good].status == "candidate"
    versions = ModelVersionRegistry.on(registry)
    assert versions.get(bad, 2).status == "failed"
    assert not versions.get(bad, 2).artifact_path.exists()


def test_named_strategies_and_statuses(env):
    _, _, registry = env
    retired = registry.register(MeanFit({"ticker": "UP.US"}), reports=[])
    seed_status(registry, retired, "retired")
    plain = registry.register(BuyAndHold({"ticker": "UP.US"}), reports=[])
    assert _run(env).outcomes == []
    named = _run(env, strategy_ids=[plain, retired])
    assert [(o.strategy_id, o.status, o.detail) for o in named.outcomes] == [
        (plain, "skipped", "strategy does not learn from data"),
        (retired, "skipped", "strategy is retired"),
    ]
    with pytest.raises(KeyError):
        _run(env, strategy_ids=["nope"])


def test_parallel_fits_match_serial(env):
    lake, state, registry = env
    a = registry.register(MeanFit({"ticker": "UP.US"}), reports=[])
    b = registry.register(MeanFit({"ticker": "DOWN.US"}), reports=[])
    summary = retrain_models(
        state, lake, registry, SETTINGS, as_of=AS_OF, universe=["UP.US", "DOWN.US"],
        actor="test", max_workers=2,
    )  # fmt: skip
    assert [o.status for o in summary.outcomes] == ["candidate", "candidate"]
    versions = ModelVersionRegistry.on(registry)
    means = {sid: versions.get(sid, 2).fit["mean_close"] for sid in (a, b)}
    assert means[a] > 150 > means[b]

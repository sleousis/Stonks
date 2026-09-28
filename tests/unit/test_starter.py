"""The starter set (complexity audit F19): simple strategies registered On
trial through the normal registry, never approved, plus a default trading
universe when none is configured."""

from __future__ import annotations

import pytest

from stonks.config_overrides import OverrideStore
from stonks.registry.store import StrategyRegistry
from stonks.starter import STARTER_UNIVERSE, STARTERS, install_starters, starter_info
from stonks.store.state import SqliteState


@pytest.fixture
def world(tmp_path):
    state = SqliteState(tmp_path / "state.sqlite")
    state.migrate()
    registry = StrategyRegistry(state=state, artifacts_dir=tmp_path / "artifacts")
    yield state, registry
    state.close()


def test_the_starters_are_registered_on_trial_and_labelled(world):
    state, registry = world
    done = install_starters(state, registry, actor="service:cli", current_universe=[])
    assert set(done.registered) == {s.id for s in STARTERS} and not done.skipped
    for handle in registry.list_all():
        assert handle.status == "shadow"  # On trial, never approved
        assert starter_info(handle.id) is not None
        registry.load(handle.id)  # each one loads and can run
    history = state.sql("SELECT kind, actor, reason FROM status_changes ORDER BY id")
    assert {r["kind"] for r in history} == {"config"}
    assert all("Starter" in r["reason"] for r in history)
    assert not state.sql("SELECT 1 FROM status_changes WHERE to_status = 'active'")


def test_the_trading_universe_is_set_only_when_empty(world):
    state, registry = world
    done = install_starters(state, registry, actor="service:cli", current_universe=[])
    assert done.universe == STARTER_UNIVERSE
    assert OverrideStore(state).values()["production.universe"] == list(STARTER_UNIVERSE)
    OverrideStore(state).reset("production.universe", actor="user:ada", reason="mine")
    again = install_starters(state, registry, actor="service:cli", current_universe=["AAPL.US"])
    assert again.universe is None
    assert "production.universe" not in OverrideStore(state).values()


def test_installing_twice_changes_nothing(world):
    state, registry = world
    install_starters(state, registry, actor="service:cli", current_universe=[])
    registry.set_status(
        "starter_momentum", "retired", actor="user:ada", reason="not for this install"
    )
    again = install_starters(state, registry, actor="service:cli", current_universe=["SPY.US"])
    assert again.registered == ()
    assert set(again.skipped) == {s.id for s in STARTERS}
    assert registry.list_all(status="retired")[0].id == "starter_momentum"


def test_other_strategies_are_not_starters():
    assert starter_info("bah_active") is None

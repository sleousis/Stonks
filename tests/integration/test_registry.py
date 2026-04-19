"""Integration tests for StrategyRegistry — the catalog of strategies that
survived the lab. Metadata in SqliteState, artifacts on disk.
"""

from __future__ import annotations

import pytest

from stonks.core.protocols import SurvivalReport
from stonks.registry.store import StrategyRegistry
from stonks.store.state import SqliteState
from stonks.strategies.examples.buy_and_hold import BuyAndHold
from stonks.strategies.examples.momentum import Momentum


@pytest.fixture
def registry(tmp_path):
    state = SqliteState(tmp_path / "state.sqlite")
    state.migrate()
    artifacts = tmp_path / "artifacts"
    yield StrategyRegistry(state=state, artifacts_dir=artifacts), state, artifacts
    state.close()


def _sample_reports():
    return [
        SurvivalReport(test_id="oos", passed=True, metrics={"sharpe_oos": 1.5}),
        SurvivalReport(test_id="drift", passed=True, metrics={"max_psi": 0.05}),
    ]


def test_register_and_list_returns_new_shadow_strategy(registry):
    reg, state, _ = registry
    strategy = BuyAndHold({"ticker": "AAPL.US", "allocation": 1.0})
    sid = reg.register(strategy, reports=_sample_reports())

    assert sid  # some id was returned
    handles = reg.list_all()
    assert len(handles) == 1
    h = handles[0]
    assert h.id == sid
    assert h.status == "shadow"             # default on fresh register
    assert h.class_path.endswith(":BuyAndHold")
    assert h.params["ticker"] == "AAPL.US"


def test_list_active_filters_out_shadow_and_retired(registry):
    reg, _, _ = registry
    active_id = reg.register(BuyAndHold({"ticker": "A.US"}), reports=_sample_reports())
    shadow_id = reg.register(BuyAndHold({"ticker": "B.US"}), reports=_sample_reports())
    retired_id = reg.register(BuyAndHold({"ticker": "C.US"}), reports=_sample_reports())

    reg.set_status(active_id, "active")
    reg.set_status(retired_id, "retired")

    active = {h.id for h in reg.list_active()}
    assert active == {active_id}
    _ = shadow_id  # intentionally untouched; stays "shadow"


def test_load_rehydrates_a_strategy(registry):
    reg, _, _ = registry
    sid = reg.register(
        Momentum({"lookback_days": 30, "threshold": 0.05}),
        reports=_sample_reports(),
    )
    loaded = reg.load(sid)
    assert isinstance(loaded, Momentum)
    assert loaded.params["lookback_days"] == 30
    assert loaded.params["threshold"] == 0.05


def test_get_reports_returns_persisted_reports(registry):
    reg, _, _ = registry
    reports = _sample_reports()
    sid = reg.register(BuyAndHold({"ticker": "AAPL.US"}), reports=reports)

    out = reg.get_reports(sid)
    assert {r.test_id for r in out} == {"oos", "drift"}
    metrics_map = {r.test_id: r.metrics for r in out}
    assert metrics_map["oos"]["sharpe_oos"] == 1.5


def test_set_status_rejects_unknown_value(registry):
    reg, _, _ = registry
    sid = reg.register(BuyAndHold({"ticker": "AAPL.US"}), reports=_sample_reports())
    with pytest.raises(ValueError):
        reg.set_status(sid, "zombie")


def test_register_writes_artifact_directory(registry):
    reg, _, artifacts_dir = registry
    sid = reg.register(BuyAndHold({"ticker": "AAPL.US"}), reports=_sample_reports())
    assert (artifacts_dir / sid).is_dir()
    assert (artifacts_dir / sid / "meta.json").exists()
    assert (artifacts_dir / sid / "params.json").exists()
    assert (artifacts_dir / sid / "reports" / "oos.json").exists()

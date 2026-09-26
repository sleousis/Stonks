"""Integration tests for StrategyRegistry — the catalog of strategies that
survived the lab. Metadata in SqliteState, artifacts on disk.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from stonks.core.protocols import SurvivalReport
from stonks.registry.artifact import ArtifactBundle
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
    assert h.status == "shadow"  # default on fresh register
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


# ---- fitted-state round trip ------------------------------------------------


class FittedBuyAndHold(BuyAndHold):
    """Module-scope strategy with fitted state persisted beside params.json,
    so the registry can re-import it via its class path."""

    fitted_value: float | None = None

    def save(self, path) -> None:
        super().save(path)
        if self.fitted_value is not None:
            (Path(path) / "fitted_state.json").write_text(
                json.dumps({"fitted_value": self.fitted_value})
            )

    @classmethod
    def load(cls, path):
        instance = super().load(path)
        state_file = Path(path) / "fitted_state.json"
        if state_file.exists():
            instance.fitted_value = json.loads(state_file.read_text())["fitted_value"]
        return instance


class NoLoadStrategy:
    """Minimal strategy-like class without save/load."""

    id = "no_load"

    def __init__(self, params) -> None:
        self.params = dict(params)


def test_register_and_load_round_trips_fitted_state(registry):
    reg, _, artifacts_dir = registry
    strategy = FittedBuyAndHold({"ticker": "AAPL.US"})
    strategy.fitted_value = 0.42
    sid = reg.register(strategy, reports=_sample_reports())

    assert (artifacts_dir / sid / "fitted_state.json").exists()
    loaded = reg.load(sid)
    assert isinstance(loaded, FittedBuyAndHold)
    assert loaded.fitted_value == 0.42
    assert loaded.params["ticker"] == "AAPL.US"


def test_register_artifact_meta_is_consistent_between_bundle_and_strategy(registry):
    reg, _, artifacts_dir = registry
    sid = reg.register(FittedBuyAndHold({"ticker": "AAPL.US"}), reports=_sample_reports())

    meta = json.loads((artifacts_dir / sid / "meta.json").read_text())
    # bundle-owned keys
    assert meta["class_path"].endswith(":FittedBuyAndHold")
    assert "created_at" in meta
    assert "stonks_version" in meta
    # strategy-owned key survives the bundle write
    assert meta["id"] == FittedBuyAndHold.id

    bundle = ArtifactBundle.load(artifacts_dir / sid)
    assert bundle.params == json.loads((artifacts_dir / sid / "params.json").read_text())
    assert bundle.params["ticker"] == "AAPL.US"
    assert {r.test_id for r in bundle.reports} == {"oos", "drift"}


def test_register_duplicate_id_raises_without_touching_existing_artifact(registry):
    reg, _, artifacts_dir = registry
    sid = reg.register(
        BuyAndHold({"ticker": "AAPL.US"}), reports=_sample_reports(), strategy_id="fixed"
    )
    params_before = (artifacts_dir / sid / "params.json").read_text()
    meta_before = (artifacts_dir / sid / "meta.json").read_text()

    with pytest.raises(ValueError, match="already registered"):
        reg.register(
            BuyAndHold({"ticker": "MSFT.US"}),
            reports=[SurvivalReport(test_id="extra", passed=False, metrics={})],
            strategy_id="fixed",
        )

    assert (artifacts_dir / sid / "params.json").read_text() == params_before
    assert (artifacts_dir / sid / "meta.json").read_text() == meta_before
    assert not (artifacts_dir / sid / "reports" / "extra.json").exists()
    assert reg.load(sid).params["ticker"] == "AAPL.US"


def test_set_status_unknown_id_raises_key_error(registry):
    reg, _, _ = registry
    with pytest.raises(KeyError):
        reg.set_status("does_not_exist", "active")


def test_load_falls_back_to_constructor_when_class_has_no_load(registry):
    reg, _, _ = registry
    sid = reg.register(NoLoadStrategy({"x": 1}), reports=_sample_reports())
    loaded = reg.load(sid)
    assert isinstance(loaded, NoLoadStrategy)
    assert loaded.params == {"x": 1}

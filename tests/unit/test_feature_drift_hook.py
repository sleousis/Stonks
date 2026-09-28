"""Live feature drift of model strategies (roadmap 23.10): PSI per feature
against the training profile stored with the model, warn only."""

from __future__ import annotations

from datetime import date, timedelta

import numpy as np
import pytest

from stonks.features.feature_profile import FeatureProfile
from stonks.production.feature_drift import (
    FeatureDriftSettings,
    check_feature_drift,
    recent_feature_rows,
)
from stonks.production.hooks import TickHookContext, registered_hooks
from stonks.production.ranker import SignalSet
from stonks.store.state import SqliteState

NAMES = ("a", "b")
START = date(2025, 1, 1)


@pytest.fixture
def state(tmp_path):
    s = SqliteState(tmp_path / "state.sqlite")
    s.migrate()
    yield s
    s.close()


class _Model:
    """A model strategy: a training profile and one live row per day."""

    def __init__(self, shift: float = 0.0, names=NAMES, seed: int = 0) -> None:
        rng = np.random.default_rng(seed)
        self._profile = FeatureProfile.build(rng.normal(size=(400, 2)), NAMES)
        self._rng = np.random.default_rng(seed + 1)
        self.shift = shift
        self.names = names
        self.calls: list[tuple[str, date]] = []

    def feature_profile(self):
        return self._profile

    def model_feature_row(self, ticker, as_of, lake):
        self.calls.append((ticker, as_of))
        if ticker != "X.US":
            return None
        v = self._rng.normal(size=2)
        return {self.names[0]: float(v[0] + self.shift), self.names[1]: float(v[1])}


class _RuleBased:
    def estimate_return(self, *a):
        return 1.0


def _run_days(state, model, days: int, settings=None, sid: str = "m1"):
    findings = []
    for k in range(days):
        findings = check_feature_drift(
            state,
            None,
            START + timedelta(days=k),
            {sid: model, "rules": _RuleBased()},
            ["X.US", "Y.US"],
            settings or FeatureDriftSettings(min_rows=20, window_days=60),
        )
    return findings


def test_rows_are_recorded_per_day_and_only_for_model_strategies(state):
    model = _Model()
    _run_days(state, model, 3)
    rows = recent_feature_rows(
        state, "m1", model.feature_profile().profile_id, START + timedelta(days=2), 60
    )
    assert len(rows) == 3
    assert list(rows[0]) == list(NAMES)
    assert {t for t, _ in model.calls} == {"X.US", "Y.US"}


def test_no_finding_until_enough_rows(state):
    findings = _run_days(state, _Model(shift=5.0), 10)
    assert findings == []


def test_stable_features_pass_and_a_shift_warns(state):
    stable = _run_days(state, _Model(), 40)
    assert [f.status for f in stable] == ["ok"]
    assert stable[0].max_psi < 0.25

    shifted = _run_days(state, _Model(shift=3.0, seed=4), 40, sid="m2")
    [finding] = shifted
    assert finding.status == "warn"
    assert finding.worst_feature == "a"
    assert finding.psi["a"] > 0.25
    assert finding.as_dict()["strategy_id"] == "m2"


def test_window_keeps_only_recent_days(state):
    model = _Model()
    _run_days(state, model, 30)
    rows = recent_feature_rows(
        state, "m1", model.feature_profile().profile_id, START + timedelta(days=29), 7
    )
    assert len(rows) == 7


def test_a_new_profile_starts_a_new_window(state):
    old = _Model(seed=0)
    _run_days(state, old, 25)
    new = _Model(seed=9)
    assert new.feature_profile().profile_id != old.feature_profile().profile_id
    assert _run_days(state, new, 5) == []


def test_live_rows_with_other_features_report_a_schema_warning(state):
    model = _Model(names=("b", "a"))
    [finding] = _run_days(state, model, 1)
    assert finding.status == "schema"
    assert "order" in finding.detail


def test_rerun_of_the_same_day_does_not_double_rows(state):
    model = _Model()
    settings = FeatureDriftSettings(min_rows=20, window_days=60)
    for _ in range(2):
        check_feature_drift(state, None, START, {"m1": model}, ["X.US"], settings)
    rows = recent_feature_rows(state, "m1", model.feature_profile().profile_id, START, 60)
    assert len(rows) == 1


def test_disabled_does_nothing(state):
    model = _Model()
    out = check_feature_drift(
        state, None, START, {"m1": model}, ["X.US"], FeatureDriftSettings(enabled=False)
    )
    assert out == [] and model.calls == []


def test_a_failing_strategy_is_reported_not_raised(state):
    class Broken(_Model):
        def model_feature_row(self, ticker, as_of, lake):
            raise RuntimeError("boom")

    [finding] = _run_days(state, Broken(), 1)
    assert finding.status == "error"
    assert "boom" in finding.detail


# ---- the hook ---------------------------------------------------------------------


def _ctx(state, as_of, model, dry_run=False):
    return TickHookContext(
        state=state,
        lake=None,  # type: ignore[arg-type]
        tick_id="t1",
        as_of=as_of,
        dry_run=dry_run,
        signals=SignalSet(as_of=as_of, instances={"m1": model}, universe=("X.US",)),
        portfolios={},
        settings=None,
    )


def test_hook_is_registered_and_warns_in_the_summary(state):
    [hook] = [h for h in registered_hooks("tick") if h.name == "feature_drift"]
    model = _Model(shift=3.0)
    out = None
    for k in range(60):
        out = hook.run(_ctx(state, START + timedelta(days=k), model))
    assert out is not None
    assert out["feature_drift"][0]["status"] == "warn"


def test_hook_skips_dry_runs_and_missing_signals(state):
    [hook] = [h for h in registered_hooks("tick") if h.name == "feature_drift"]
    model = _Model()
    assert hook.run(_ctx(state, START, model, dry_run=True)) is None
    assert model.calls == []
    ctx = _ctx(state, START, model)
    empty = TickHookContext(**{**ctx.__dict__, "signals": None})
    assert hook.run(empty) is None


def test_hook_reads_settings_from_the_tick(state):
    [hook] = [h for h in registered_hooks("tick") if h.name == "feature_drift"]
    model = _Model()

    class Settings:
        feature_drift = FeatureDriftSettings(enabled=False)

    ctx = TickHookContext(**{**_ctx(state, START, model).__dict__, "settings": Settings()})
    assert hook.run(ctx) is None
    assert model.calls == []


# ---- settings ---------------------------------------------------------------------


def test_settings_read_from_toml_and_reach_the_tick(tmp_path):
    from pathlib import Path

    from stonks.config import load_settings
    from stonks.production.settings_builder import build_tick_settings

    assert load_settings(config_path=Path("config/default.toml")).production.feature_drift == (
        FeatureDriftSettings()
    )
    cfg = tmp_path / "cfg.toml"
    cfg.write_text("[production.feature_drift]\nenabled = false\nwarn_psi = 0.4\n")
    s = load_settings(config_path=cfg)
    assert (s.production.feature_drift.enabled, s.production.feature_drift.warn_psi) == (
        False,
        0.4,
    )
    tick = build_tick_settings(s, ["X.US"])
    assert tick.feature_drift == s.production.feature_drift

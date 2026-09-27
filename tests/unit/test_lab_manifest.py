"""Reproducibility manifest (BL-06): git identity, config hash, versions, seeds."""

from __future__ import annotations

import subprocess
from datetime import date

import pytest

from stonks.backtest.costs import CostModelSettings
from stonks.config import Settings
from stonks.lab import manifest as manifest_mod
from stonks.lab.dataset import LabDataset
from stonks.lab.manifest import (
    build_manifest,
    canonical_settings_json,
    collect_seeds,
    config_hash,
    git_info,
)


def _settings(**kw) -> Settings:
    s = Settings()
    s.sources.eodhd.api_key = kw.get("api_key", "SUPERSECRET-EODHD")
    s.notify.webhook.url = kw.get("webhook", "https://hooks.example/T0K3N")
    s.production.initial_cash = kw.get("cash", 100_000.0)
    return s


def test_secrets_are_absent_from_the_hash_input():
    text = canonical_settings_json(_settings())
    assert "SUPERSECRET-EODHD" not in text
    assert "T0K3N" not in text


def test_config_hash_ignores_secrets_but_tracks_real_config():
    base = config_hash(_settings())
    assert len(base) == 64
    assert config_hash(_settings(api_key="another-key")) == base
    assert config_hash(_settings(cash=5.0)) != base


def test_config_hash_is_none_without_settings():
    assert config_hash(None) is None


def test_git_info_reports_sha_and_dirty_flag():
    info = git_info()
    # Tests run inside the repo checkout; tolerate an exported tree.
    assert set(info) == {"git_sha", "git_dirty"}
    if info["git_sha"] is not None:
        assert len(info["git_sha"]) == 40
        assert isinstance(info["git_dirty"], bool)


def test_no_git_gives_none(monkeypatch):
    def _missing(*a, **k):
        raise FileNotFoundError("git")

    monkeypatch.setattr(manifest_mod.subprocess, "run", _missing)
    assert git_info() == {"git_sha": None, "git_dirty": None}


def test_git_failure_gives_none(monkeypatch):
    def _fail(*a, **k):
        return subprocess.CompletedProcess(a, 128, stdout="", stderr="not a git repository")

    monkeypatch.setattr(manifest_mod.subprocess, "run", _fail)
    assert git_info() == {"git_sha": None, "git_dirty": None}


class _Seeded:
    id = "mcpt"

    def __init__(self, seed):
        self._seed = seed


class _PublicSeed:
    seed = 5


class _NoSeed:
    id = "oos"


def test_collect_seeds_from_the_tuner_and_the_tests():
    seeds = collect_seeds(_PublicSeed(), [_Seeded(7), _NoSeed()])
    assert seeds == {"tuner": 5, "tests": {"mcpt": 7}}


def test_build_manifest_without_a_lake():
    ds = LabDataset(
        lake=None,
        universe=["X.US"],
        start=date(2024, 1, 1),
        end=date(2024, 6, 1),
        costs=CostModelSettings(),
    )
    m = build_manifest(_settings(), ds, {"tuner": 1})
    assert m["seeds"] == {"tuner": 1}
    assert m["config_hash"] == config_hash(_settings())
    assert m["data_fingerprint"] is None
    assert m["costs"] == CostModelSettings().model_dump(mode="json")
    assert m["stonks_version"]
    assert m["libraries"]["numpy"]
    assert m["python_version"]
    assert "git_sha" in m and "git_dirty" in m
    assert m["dataset"]["universe"] == ["X.US"]
    assert m["dataset"]["full_window"] == ["2024-01-01", "2024-06-01"]


def test_build_manifest_is_json_serialisable():
    import json

    ds = LabDataset(lake=None, universe=["X.US"], start=date(2024, 1, 1), end=date(2024, 6, 1))
    manifest = build_manifest(None, ds, {})
    assert json.loads(json.dumps(manifest))["dataset"]["universe"] == ["X.US"]


@pytest.mark.parametrize("dataset", [None, object()])
def test_build_manifest_tolerates_a_missing_or_odd_dataset(dataset):
    m = build_manifest(None, dataset, {})
    assert m["data_fingerprint"] is None

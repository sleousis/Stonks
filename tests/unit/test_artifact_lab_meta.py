"""ArtifactBundle carries lab provenance in meta.json (BL-04, BL-06)."""

from __future__ import annotations

import json

from stonks.registry.artifact import ArtifactBundle, update_meta

LAB_META = {
    "lab_run_id": "lab_1",
    "n_trials_total": 40,
    "hypothesis": "trend persists",
    "premortem": "it is beta",
    "manifest": {"git_sha": "abc", "seeds": {"tuner": 0}},
}


def _meta(path):
    return json.loads((path / "meta.json").read_text())


def test_bundle_writes_extra_meta(tmp_path):
    ArtifactBundle(path=tmp_path, class_path="m:C", params={}, meta=dict(LAB_META)).save()
    meta = _meta(tmp_path)
    for key, value in LAB_META.items():
        assert meta[key] == value
    assert meta["class_path"] == "m:C"


def test_registry_keys_win_over_extra_meta(tmp_path):
    ArtifactBundle(path=tmp_path, class_path="m:C", params={}, meta={"class_path": "x:Y"}).save()
    assert _meta(tmp_path)["class_path"] == "m:C"


def test_load_round_trips_extra_meta(tmp_path):
    ArtifactBundle(path=tmp_path, class_path="m:C", params={"a": 1}, meta=dict(LAB_META)).save()
    loaded = ArtifactBundle.load(tmp_path)
    assert loaded.meta == LAB_META
    assert loaded.params == {"a": 1}


def test_bundle_without_extra_meta_is_unchanged(tmp_path):
    ArtifactBundle(path=tmp_path, class_path="m:C", params={}).save()
    assert set(_meta(tmp_path)) == {"class_path", "created_at", "stonks_version"}
    assert ArtifactBundle.load(tmp_path).meta == {}


def test_update_meta_merges_into_an_existing_bundle(tmp_path):
    ArtifactBundle(path=tmp_path, class_path="m:C", params={}).save()
    (tmp_path / "meta.json").write_text(json.dumps({**_meta(tmp_path), "strategy_key": "kept"}))
    update_meta(tmp_path, LAB_META)
    meta = _meta(tmp_path)
    assert meta["lab_run_id"] == "lab_1"
    assert meta["strategy_key"] == "kept"
    assert meta["class_path"] == "m:C"


def test_update_meta_never_overwrites_registry_keys(tmp_path):
    ArtifactBundle(path=tmp_path, class_path="m:C", params={}).save()
    update_meta(tmp_path, {"class_path": "evil:X", "lab_run_id": "r"})
    assert _meta(tmp_path)["class_path"] == "m:C"

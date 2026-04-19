"""Unit tests for the NameMapper — a simple, dict-driven key renamer used by data
sources to translate vendor field names into the canonical schema."""

from stonks.ingest.name_mapper import rename_keys


def test_rename_maps_known_keys():
    mapping = {"adjusted_close": "adj_close"}
    out = rename_keys({"adjusted_close": 123.0, "close": 100.0}, mapping)
    assert out == {"adj_close": 123.0, "close": 100.0}


def test_rename_leaves_unknown_keys_intact():
    mapping = {"adjusted_close": "adj_close"}
    out = rename_keys({"volume": 1000}, mapping)
    assert out == {"volume": 1000}


def test_rename_is_pure():
    src = {"a": 1, "b": 2}
    rename_keys(src, {"a": "alpha"})
    assert src == {"a": 1, "b": 2}  # input not mutated


def test_rename_empty_mapping_is_identity():
    src = {"x": 1, "y": 2}
    assert rename_keys(src, {}) == src


def test_rename_empty_dict():
    assert rename_keys({}, {"a": "b"}) == {}

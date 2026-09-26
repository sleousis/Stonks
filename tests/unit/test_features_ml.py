"""Classifier / Clusterer seam over scikit-learn (features.ml)."""

from __future__ import annotations

import json

import numpy as np
import pytest

from stonks.features.ml import (
    Classifier,
    Clusterer,
    Clustering,
    ForestClassifier,
    SilhouetteKMeans,
)


def _separable(n=200, seed=0):
    rng = np.random.default_rng(seed)
    x = rng.normal(size=(n, 3))
    y = (x[:, 0] + 0.2 * rng.normal(size=n) > 0).astype(int)
    return x, y


# ---- classifier ---------------------------------------------------------------


def test_forest_classifier_learns_and_returns_class_one_probabilities():
    x, y = _separable()
    clf = ForestClassifier(n_estimators=50, max_depth=3, seed=1)
    assert isinstance(clf, Classifier)
    clf.fit(x, y)
    p = clf.predict_proba(np.array([[3.0, 0.0, 0.0], [-3.0, 0.0, 0.0]]))
    assert isinstance(p, np.ndarray) and p.shape == (2,)
    assert p[0] > 0.7 and p[1] < 0.3


def test_forest_classifier_is_deterministic_for_a_seed():
    x, y = _separable()
    a = ForestClassifier(n_estimators=30, seed=7)
    b = ForestClassifier(n_estimators=30, seed=7)
    a.fit(x, y)
    b.fit(x, y)
    np.testing.assert_array_equal(a.predict_proba(x), b.predict_proba(x))


def test_single_class_training_gives_constant_probability():
    x, _ = _separable()
    clf = ForestClassifier(n_estimators=10)
    clf.fit(x, np.zeros(len(x), dtype=int))
    assert np.all(clf.predict_proba(x[:5]) == 0.0)
    clf.fit(x, np.ones(len(x), dtype=int))
    assert np.all(clf.predict_proba(x[:5]) == 1.0)


def test_unfitted_classifier_refuses_to_predict_or_save(tmp_path):
    clf = ForestClassifier()
    with pytest.raises(RuntimeError):
        clf.predict_proba(np.zeros((1, 3)))
    with pytest.raises(RuntimeError):
        clf.save(tmp_path / "m")


def test_forest_classifier_round_trips(tmp_path):
    x, y = _separable()
    clf = ForestClassifier(n_estimators=40, max_depth=2, seed=3)
    clf.fit(x, y)
    clf.save(tmp_path / "model")
    meta = json.loads((tmp_path / "model" / "model.json").read_text())
    assert meta["n_estimators"] == 40 and meta["max_depth"] == 2 and meta["seed"] == 3
    loaded = ForestClassifier.load(tmp_path / "model")
    np.testing.assert_array_equal(loaded.predict_proba(x), clf.predict_proba(x))


def test_load_refuses_a_model_file_whose_digest_does_not_match(tmp_path):
    x, y = _separable()
    clf = ForestClassifier(n_estimators=5)
    clf.fit(x, y)
    clf.save(tmp_path / "model")
    blob = tmp_path / "model" / "model.joblib"
    blob.write_bytes(blob.read_bytes() + b"tampered")
    with pytest.raises(ValueError, match="digest"):
        ForestClassifier.load(tmp_path / "model")


def test_load_refuses_a_model_file_name_that_escapes_the_directory(tmp_path):
    x, y = _separable()
    clf = ForestClassifier(n_estimators=5)
    clf.fit(x, y)
    clf.save(tmp_path / "model")
    meta_path = tmp_path / "model" / "model.json"
    meta = json.loads(meta_path.read_text())
    meta["file"] = "../evil.joblib"
    meta_path.write_text(json.dumps(meta))
    with pytest.raises(ValueError, match="file"):
        ForestClassifier.load(tmp_path / "model")


# ---- clusterer ----------------------------------------------------------------


def _blobs(seed=0):
    rng = np.random.default_rng(seed)
    centers = np.array([[0.0, 0.0], [10.0, 0.0], [0.0, 10.0]])
    return np.vstack([c + rng.normal(scale=0.3, size=(40, 2)) for c in centers])


def test_silhouette_kmeans_picks_the_natural_cluster_count():
    x = _blobs()
    km = SilhouetteKMeans(k_min=2, k_max=6, seed=0)
    assert isinstance(km, Clusterer)
    result = km.fit(x)
    assert isinstance(result, Clustering)
    assert result.k == 3
    assert result.centers.shape == (3, 2)
    assert result.labels.shape == (len(x),)
    assert isinstance(result.centers, np.ndarray)


def test_silhouette_kmeans_is_deterministic():
    x = _blobs(1)
    a = SilhouetteKMeans(k_min=2, k_max=8, seed=5).fit(x)
    b = SilhouetteKMeans(k_min=2, k_max=8, seed=5).fit(x)
    np.testing.assert_array_equal(a.centers, b.centers)
    np.testing.assert_array_equal(a.labels, b.labels)


def test_silhouette_kmeans_clips_k_max_to_the_sample_count():
    x = _blobs()[:8]
    result = SilhouetteKMeans(k_min=2, k_max=40, seed=0).fit(x)
    assert 2 <= result.k <= 7


def test_silhouette_kmeans_needs_more_samples_than_k_min():
    with pytest.raises(ValueError, match="samples"):
        SilhouetteKMeans(k_min=5, k_max=10).fit(np.zeros((5, 2)))

"""Feature importance under purged CV: MDA, SFI and clustered MDA
(roadmap 23.10, AFML ch. 8)."""

from __future__ import annotations

import json

import numpy as np
import pytest

from stonks.features.ml import ForestClassifier
from stonks.lab.importance import (
    ImportanceReport,
    TrainingSet,
    clustered_mda,
    feature_clusters,
    feature_importance,
    mda,
    sfi,
)
from stonks.reporting.importance import render_importance_page, render_importance_section


def _data(n: int = 400, seed: int = 0) -> TrainingSet:
    """``signal`` drives the label, ``twin`` is a noisy copy of it and
    ``noise`` is pure noise."""
    rng = np.random.default_rng(seed)
    signal = rng.normal(size=n)
    twin = signal + rng.normal(0, 0.1, n)
    noise = rng.normal(size=n)
    y = (signal + rng.normal(0, 0.5, n) > 0).astype(int)
    t0 = np.arange(n)
    return TrainingSet(
        x=np.column_stack([signal, twin, noise]),
        y=y,
        t0=t0,
        t1=t0 + 2,
        feature_names=("signal", "twin", "noise"),
    )


def _forest():
    return ForestClassifier(n_estimators=40, max_depth=3, seed=0)


def test_training_set_validates_shapes():
    data = _data(50)
    with pytest.raises(ValueError):
        TrainingSet(x=data.x[:, :2], y=data.y, t0=data.t0, t1=data.t1, feature_names=("a",))
    with pytest.raises(ValueError):
        TrainingSet(
            x=data.x, y=data.y[:-1], t0=data.t0, t1=data.t1, feature_names=data.feature_names
        )


def test_sfi_ranks_the_signal_above_the_noise():
    table = sfi(_forest, _data(), folds=4)
    by = {r.name: r for r in table.rows}
    assert by["signal"].mean > by["noise"].mean
    assert by["twin"].mean > by["noise"].mean
    assert table.method == "sfi"
    assert table.rows[0].name in {"signal", "twin"}  # sorted, best first


def test_mda_puts_noise_near_zero():
    table = mda(_forest, _data(), folds=4, seed=1)
    by = {r.name: r for r in table.rows}
    assert abs(by["noise"].mean) < 0.05
    assert by["signal"].mean + by["twin"].mean > by["noise"].mean


def test_substitution_hides_twins_from_mda_but_not_from_clustered_mda():
    data = _data()
    clusters = [["signal", "twin"], ["noise"]]
    table = clustered_mda(_forest, data, clusters=clusters, folds=4, seed=1)
    by = {r.name: r for r in table.rows}
    assert set(by) == {"signal+twin", "noise"}
    assert by["signal+twin"].members == ("signal", "twin")
    assert by["signal+twin"].mean > 0.1
    assert by["signal+twin"].mean > by["noise"].mean


def test_feature_clusters_group_correlated_features():
    groups = feature_clusters(_data().x, ("signal", "twin", "noise"))
    as_sets = sorted(sorted(g) for g in groups)
    assert ["signal", "twin"] in as_sets
    assert ["noise"] in as_sets


def test_feature_clusters_of_two_features_keep_them_apart():
    data = _data()
    assert feature_clusters(data.x[:, :2], ("signal", "twin")) == [["signal"], ["twin"]]


def test_importance_uses_purged_folds_only():
    """Every model is fitted on rows outside its test fold and outside the
    purge around it."""
    data = _data(120)
    row_of = {float(v): i for i, v in enumerate(data.x[:, 0])}
    seen: list[set[int]] = []

    class Spy(ForestClassifier):
        def fit(self, x, y, sample_weight=None):
            if x.shape[1] == 3:
                seen.append({row_of[float(v)] for v in x[:, 0]})
            super().fit(x, y, sample_weight)

    mda(lambda: Spy(n_estimators=10), data, folds=4, seed=0)
    assert len(seen) == 4
    for train, lo in zip(seen, range(0, 120, 30), strict=True):
        assert not train & set(range(max(0, lo - 2), lo + 30 + 2))


def test_accuracy_scoring_is_available():
    table = sfi(_forest, _data(), folds=3, scoring="accuracy")
    assert all(0.0 <= r.mean <= 1.0 for r in table.rows)
    with pytest.raises(ValueError):
        sfi(_forest, _data(), folds=3, scoring="auc")


def test_mda_is_deterministic_for_a_seed():
    a = mda(_forest, _data(), folds=3, seed=3)
    b = mda(_forest, _data(), folds=3, seed=3)
    assert a.as_dict() == b.as_dict()


# ---- the lab tool and its report ------------------------------------------------


class _ModelStrategy:
    id = "toy_model"
    params: dict = {}

    def training_set(self, dataset):
        return _data(200)

    def new_classifier(self):
        return _forest()


def test_feature_importance_runs_every_method_on_a_model_strategy():
    report = feature_importance(_ModelStrategy(), dataset=None, folds=3, seed=0)
    assert isinstance(report, ImportanceReport)
    assert [t.method for t in report.tables] == ["mda", "sfi", "clustered_mda"]
    assert report.n_samples == 200
    assert report.strategy_id == "toy_model"
    payload = json.loads(json.dumps(report.as_dict()))
    assert payload["tables"][0]["rows"]


def test_feature_importance_refuses_a_strategy_without_a_training_set():
    class Plain:
        id = "plain"

    with pytest.raises(TypeError, match="training_set"):
        feature_importance(Plain(), dataset=None)


def test_report_section_renders_every_table_and_escapes_names():
    report = feature_importance(_ModelStrategy(), dataset=None, folds=3, methods=("sfi",))
    report = ImportanceReport(
        strategy_id="<b>x</b>",
        n_samples=report.n_samples,
        folds=report.folds,
        embargo_pct=report.embargo_pct,
        scoring=report.scoring,
        tables=report.tables,
    )
    html = render_importance_section(report)
    assert "<section>" in html and "SFI" in html
    assert "&lt;b&gt;x&lt;/b&gt;" in html and "<b>x</b>" not in html
    page = render_importance_page(report)
    assert page.startswith("<!doctype html>")

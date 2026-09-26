"""Machine-learning seams: :class:`Classifier` and :class:`Clusterer`.

scikit-learn is the backend, but its types never leave this module: models
take and return plain numpy arrays, and persistence is behind ``save`` /
``load``. Swapping the backend (LightGBM, a hand-rolled model, ...) means a
new subclass here and no change in any strategy.

Persistence and trust: a fitted forest is saved with ``joblib`` (a pickle
format; loading one can run arbitrary code). :meth:`ForestClassifier.load`
therefore only reads a fixed file name from inside the given directory,
refuses symlinks and names that escape it, and checks the SHA-256 digest
recorded in the JSON sidecar before unpickling. That guards against
corruption and swapped files, not against someone who can write the whole
directory: only load artifact directories this system wrote (the registry's
``data/artifacts/<id>/``), never a path from user input.
"""

from __future__ import annotations

import hashlib
import json
from abc import ABC, abstractmethod
from dataclasses import dataclass
from pathlib import Path

import numpy as np

__all__ = ["Classifier", "Clusterer", "Clustering", "ForestClassifier", "SilhouetteKMeans"]

_MODEL_FILE = "model.joblib"
_META_FILE = "model.json"


# ---- classifier ---------------------------------------------------------------


class Classifier(ABC):
    """Binary classifier over a numeric feature matrix."""

    @abstractmethod
    def fit(self, x: np.ndarray, y: np.ndarray) -> None: ...

    @abstractmethod
    def predict_proba(self, x: np.ndarray) -> np.ndarray:
        """``P(y == 1)`` per row, shape ``(n,)``."""

    @abstractmethod
    def save(self, path: Path) -> None:
        """Persist into directory ``path`` (created if missing)."""

    @classmethod
    @abstractmethod
    def load(cls, path: Path) -> Classifier: ...


class ForestClassifier(Classifier):
    """Random forest (scikit-learn ``RandomForestClassifier``) with a fixed
    seed, so the same data always gives the same model."""

    kind = "random_forest"

    def __init__(self, n_estimators: int = 300, max_depth: int | None = 3, seed: int = 0) -> None:
        self.n_estimators = int(n_estimators)
        self.max_depth = None if max_depth is None else int(max_depth)
        self.seed = int(seed)
        self._model = None
        # training labels had a single class: sklearn can't give P(1) then
        self._constant: float | None = None

    @property
    def is_fitted(self) -> bool:
        return self._model is not None or self._constant is not None

    def fit(self, x: np.ndarray, y: np.ndarray) -> None:
        from sklearn.ensemble import RandomForestClassifier

        x = np.asarray(x, dtype=float)
        y = np.asarray(y, dtype=int)
        if len(x) == 0 or len(x) != len(y):
            raise ValueError("x and y must be non-empty and of equal length")
        classes = np.unique(y)
        if len(classes) == 1:
            self._model = None
            self._constant = 1.0 if classes[0] == 1 else 0.0
            return
        model = RandomForestClassifier(
            n_estimators=self.n_estimators,
            max_depth=self.max_depth,
            random_state=self.seed,
            n_jobs=1,
        )
        model.fit(x, y)
        self._model = model
        self._constant = None

    def predict_proba(self, x: np.ndarray) -> np.ndarray:
        if not self.is_fitted:
            raise RuntimeError("classifier is not fitted")
        x = np.atleast_2d(np.asarray(x, dtype=float))
        if self._model is None:
            return np.full(len(x), float(self._constant))
        proba = self._model.predict_proba(x)
        col = list(self._model.classes_).index(1)
        return np.asarray(proba[:, col], dtype=float)

    def save(self, path: Path) -> None:
        if not self.is_fitted:
            raise RuntimeError("cannot save an unfitted classifier")
        import joblib

        path = Path(path)
        path.mkdir(parents=True, exist_ok=True)
        meta: dict[str, object] = {
            "kind": self.kind,
            "n_estimators": self.n_estimators,
            "max_depth": self.max_depth,
            "seed": self.seed,
            "constant": self._constant,
            "file": None,
            "sha256": None,
        }
        if self._model is not None:
            blob = path / _MODEL_FILE
            joblib.dump(self._model, blob)
            meta["file"] = _MODEL_FILE
            meta["sha256"] = _sha256(blob)
        (path / _META_FILE).write_text(json.dumps(meta, indent=2, sort_keys=True))

    @classmethod
    def load(cls, path: Path) -> ForestClassifier:
        path = Path(path)
        meta = json.loads((path / _META_FILE).read_text())
        if meta.get("kind") != cls.kind:
            raise ValueError(f"not a {cls.kind} model: {meta.get('kind')!r}")
        instance = cls(meta["n_estimators"], meta["max_depth"], meta["seed"])
        if meta.get("file") is None:
            if meta.get("constant") is None:
                raise ValueError("model metadata has neither a model file nor a constant")
            instance._constant = float(meta["constant"])
            return instance
        if meta["file"] != _MODEL_FILE:
            raise ValueError(f"unexpected model file name {meta['file']!r}")
        blob = path / _MODEL_FILE
        if blob.is_symlink() or not blob.is_file():
            raise ValueError(f"model file {blob} is missing or not a regular file")
        if blob.resolve().parent != path.resolve():
            raise ValueError(f"model file {blob} escapes {path}")
        if _sha256(blob) != meta.get("sha256"):
            raise ValueError(f"model file {blob} digest does not match its metadata")
        import joblib

        instance._model = joblib.load(blob)
        return instance


def _sha256(path: Path) -> str:
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


# ---- clusterer ----------------------------------------------------------------


@dataclass(frozen=True)
class Clustering:
    centers: np.ndarray  # (k, n_features)
    labels: np.ndarray  # (n_samples,) cluster index per sample
    k: int
    score: float  # silhouette of the chosen k


class Clusterer(ABC):
    @abstractmethod
    def fit(self, x: np.ndarray) -> Clustering: ...


class SilhouetteKMeans(Clusterer):
    """k-means (scikit-learn) for every ``k`` in ``[k_min, k_max]``; keeps
    the ``k`` with the best silhouette score (ties -> smaller ``k``).
    ``k_max`` is clipped to ``n_samples - 1``. Fixed seed, so repeatable.
    Silhouettes are scored on at most ``silhouette_sample`` points."""

    def __init__(
        self,
        k_min: int = 5,
        k_max: int = 40,
        seed: int = 0,
        n_init: int = 10,
        silhouette_sample: int = 3000,
    ) -> None:
        if k_min < 2 or k_max < k_min:
            raise ValueError(f"need 2 <= k_min <= k_max, got {k_min}, {k_max}")
        self.k_min = int(k_min)
        self.k_max = int(k_max)
        self.seed = int(seed)
        self.n_init = int(n_init)
        self.silhouette_sample = int(silhouette_sample)

    def fit(self, x: np.ndarray) -> Clustering:
        from sklearn.cluster import KMeans
        from sklearn.metrics import silhouette_score

        x = np.asarray(x, dtype=float)
        n = len(x)
        if n <= self.k_min:
            raise ValueError(f"need more than k_min={self.k_min} samples, got {n}")
        best: Clustering | None = None
        sample = min(n, self.silhouette_sample)
        for k in range(self.k_min, min(self.k_max, n - 1) + 1):
            km = KMeans(n_clusters=k, n_init=self.n_init, random_state=self.seed)
            labels = km.fit_predict(x)
            if len(np.unique(labels)) < 2:
                continue
            score = float(
                silhouette_score(
                    x, labels, sample_size=sample if sample < n else None, random_state=self.seed
                )
            )
            if best is None or score > best.score:
                best = Clustering(
                    centers=np.asarray(km.cluster_centers_, dtype=float).copy(),
                    labels=np.asarray(labels, dtype=int).copy(),
                    k=k,
                    score=score,
                )
        if best is None:
            raise ValueError("could not form at least two distinct clusters")
        return best

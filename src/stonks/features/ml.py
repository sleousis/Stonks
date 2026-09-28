"""Machine-learning seams: :class:`Classifier`, :class:`Regressor` and
:class:`Clusterer`, plus bet sizing from a predicted probability (BL-45).

Regressors (roadmap 23.12) are built by kind through :func:`make_regressor`.
One kind exists today, ``hist_gbm``: scikit-learn's histogram gradient
boosting. It takes missing values as they are and runs with early stopping
off, so no unpurged random split ever picks the model. LightGBM is left out
on purpose: its wheel needs ``libgomp``, which the slim Docker image lacks.
A LightGBM kind would be one more subclass registered here.

scikit-learn is the backend, but its types never leave this module: models
take and return plain numpy arrays, and persistence is behind ``save`` /
``load``. Swapping the backend (LightGBM, a hand-rolled model, ...) means a
new subclass here and no change in any strategy.

Bet sizing (López de Prado, *AFML* ch. 10): :func:`bet_size` turns a
classifier's ``P(win)`` into a signed size in ``[-1, 1]``,
``2 * Phi((p - 1/K) / sqrt(p (1 - p))) - 1``, discretised to steps of 0.1
so small changes in ``p`` do not trade. :func:`break_even_probability` is
the ``P(win)`` at which a take profit ``tp`` and a stop ``sl`` have zero
expectancy, ``sl / (tp + sl)``: a meta-label threshold below it takes
losing bets on average.

Classifiers accept ``sample_weight`` (for example the average uniqueness of
overlapping labels, :func:`stonks.features.labels.avg_uniqueness`).

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
from typing import Any, cast

import numpy as np
from scipy.stats import norm

__all__ = [
    "Classifier",
    "Clusterer",
    "Clustering",
    "ForestClassifier",
    "GradientBoostingRegressor",
    "Regressor",
    "SilhouetteKMeans",
    "bet_size",
    "break_even_probability",
    "load_regressor",
    "make_regressor",
    "regressor_kinds",
]

_MODEL_FILE = "model.joblib"
_META_FILE = "model.json"


# ---- classifier ---------------------------------------------------------------


class Classifier(ABC):
    """Binary classifier over a numeric feature matrix."""

    @abstractmethod
    def fit(self, x: np.ndarray, y: np.ndarray, sample_weight: np.ndarray | None = None) -> None:
        """Fit on ``x`` and ``y``; ``sample_weight`` weighs each row."""

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

    def fit(self, x: np.ndarray, y: np.ndarray, sample_weight: np.ndarray | None = None) -> None:
        from sklearn.ensemble import RandomForestClassifier

        x = np.asarray(x, dtype=float)
        y = np.asarray(y, dtype=int)
        if len(x) == 0 or len(x) != len(y):
            raise ValueError("x and y must be non-empty and of equal length")
        weights = None if sample_weight is None else np.asarray(sample_weight, dtype=float)
        if weights is not None and weights.shape != (len(y),):
            raise ValueError("sample_weight must have one weight per row")
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
        model.fit(x, y, sample_weight=weights)
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
            meta["file"], meta["sha256"] = _dump_blob(path, self._model)
        (path / _META_FILE).write_text(json.dumps(meta, indent=2, sort_keys=True), encoding="utf-8")

    @classmethod
    def load(cls, path: Path) -> ForestClassifier:
        path = Path(path)
        meta = json.loads((path / _META_FILE).read_text(encoding="utf-8"))
        if meta.get("kind") != cls.kind:
            raise ValueError(f"not a {cls.kind} model: {meta.get('kind')!r}")
        instance = cls(meta["n_estimators"], meta["max_depth"], meta["seed"])
        if meta.get("file") is None:
            if meta.get("constant") is None:
                raise ValueError("model metadata has neither a model file nor a constant")
            instance._constant = float(meta["constant"])
            return instance
        instance._model = _load_blob(path, meta)
        return instance


def _sha256(path: Path) -> str:
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def _dump_blob(path: Path, model: Any) -> tuple[str, str]:
    """Save ``model`` with joblib inside ``path``: ``(file name, digest)``."""
    import joblib

    blob = Path(path) / _MODEL_FILE
    joblib.dump(model, blob)
    return _MODEL_FILE, _sha256(blob)


def _load_blob(path: Path, meta: dict[str, Any]) -> Any:
    """The joblib model a sidecar ``meta`` names inside ``path``, after the
    name, symlink, containment and digest checks of the module doc."""
    path = Path(path)
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

    return joblib.load(blob)


# ---- regressor ----------------------------------------------------------------


class Regressor(ABC):
    """Regression over a numeric feature matrix (missing values allowed)."""

    kind: str = ""

    @abstractmethod
    def fit(self, x: np.ndarray, y: np.ndarray, sample_weight: np.ndarray | None = None) -> None:
        """Fit on ``x`` and ``y``. ``sample_weight`` weighs each row."""

    @abstractmethod
    def predict(self, x: np.ndarray) -> np.ndarray:
        """One prediction per row, shape ``(n,)``."""

    @abstractmethod
    def hyperparameters(self) -> dict[str, Any]:
        """The settings the model was built with, as plain JSON."""

    @abstractmethod
    def save(self, path: Path) -> None:
        """Persist into directory ``path`` (created if missing)."""

    @classmethod
    @abstractmethod
    def load(cls, path: Path) -> Regressor: ...


class GradientBoostingRegressor(Regressor):
    """Histogram gradient boosting (scikit-learn
    ``HistGradientBoostingRegressor``) with a fixed seed and no early
    stopping, so the same data always gives the same model."""

    kind = "hist_gbm"

    def __init__(
        self,
        max_iter: int = 200,
        learning_rate: float = 0.05,
        max_leaf_nodes: int = 15,
        min_samples_leaf: int = 50,
        l2_regularization: float = 1.0,
        seed: int = 0,
    ) -> None:
        if int(max_iter) < 1:
            raise ValueError(f"max_iter must be >= 1, got {max_iter}")
        if not float(learning_rate) > 0:
            raise ValueError(f"learning_rate must be positive, got {learning_rate}")
        if int(max_leaf_nodes) < 2:
            raise ValueError(f"max_leaf_nodes must be >= 2, got {max_leaf_nodes}")
        if int(min_samples_leaf) < 1:
            raise ValueError(f"min_samples_leaf must be >= 1, got {min_samples_leaf}")
        if float(l2_regularization) < 0:
            raise ValueError(f"l2_regularization must be >= 0, got {l2_regularization}")
        self.max_iter = int(max_iter)
        self.learning_rate = float(learning_rate)
        self.max_leaf_nodes = int(max_leaf_nodes)
        self.min_samples_leaf = int(min_samples_leaf)
        self.l2_regularization = float(l2_regularization)
        self.seed = int(seed)
        self._model: Any = None
        # a constant target: nothing to learn, so predict it
        self._constant: float | None = None

    @property
    def is_fitted(self) -> bool:
        return self._model is not None or self._constant is not None

    def hyperparameters(self) -> dict[str, Any]:
        return {
            "max_iter": self.max_iter,
            "learning_rate": self.learning_rate,
            "max_leaf_nodes": self.max_leaf_nodes,
            "min_samples_leaf": self.min_samples_leaf,
            "l2_regularization": self.l2_regularization,
            "seed": self.seed,
        }

    def fit(self, x: np.ndarray, y: np.ndarray, sample_weight: np.ndarray | None = None) -> None:
        from sklearn.ensemble import HistGradientBoostingRegressor

        x = np.asarray(x, dtype=float)
        y = np.asarray(y, dtype=float)
        if x.ndim != 2 or len(x) == 0 or len(x) != len(y):
            raise ValueError("x must be a non-empty matrix with one target per row")
        if not np.isfinite(y).all():
            raise ValueError("targets must be finite")
        weights = None if sample_weight is None else np.asarray(sample_weight, dtype=float)
        if weights is not None and weights.shape != (len(y),):
            raise ValueError("sample_weight must have one weight per row")
        if np.ptp(y) == 0:
            self._model, self._constant = None, float(y[0])
            return
        model = HistGradientBoostingRegressor(
            loss="squared_error",
            max_iter=self.max_iter,
            learning_rate=self.learning_rate,
            max_leaf_nodes=self.max_leaf_nodes,
            min_samples_leaf=self.min_samples_leaf,
            l2_regularization=self.l2_regularization,
            early_stopping=cast(Any, False),
            random_state=self.seed,
        )
        model.fit(x, y, sample_weight=weights)
        self._model, self._constant = model, None

    def predict(self, x: np.ndarray) -> np.ndarray:
        if not self.is_fitted:
            raise RuntimeError("regressor is not fitted")
        x = np.atleast_2d(np.asarray(x, dtype=float))
        if self._model is None:
            return np.full(len(x), cast(float, self._constant))
        return np.asarray(self._model.predict(x), dtype=float)

    def save(self, path: Path) -> None:
        if not self.is_fitted:
            raise RuntimeError("cannot save an unfitted regressor")
        path = Path(path)
        path.mkdir(parents=True, exist_ok=True)
        meta: dict[str, object] = {
            "kind": self.kind,
            **self.hyperparameters(),
            "constant": self._constant,
            "file": None,
            "sha256": None,
        }
        if self._model is not None:
            meta["file"], meta["sha256"] = _dump_blob(path, self._model)
        (path / _META_FILE).write_text(json.dumps(meta, indent=2, sort_keys=True), encoding="utf-8")

    @classmethod
    def load(cls, path: Path) -> GradientBoostingRegressor:
        path = Path(path)
        meta = json.loads((path / _META_FILE).read_text(encoding="utf-8"))
        if meta.get("kind") != cls.kind:
            raise ValueError(f"not a {cls.kind} model: {meta.get('kind')!r}")
        instance = cls(
            max_iter=meta["max_iter"],
            learning_rate=meta["learning_rate"],
            max_leaf_nodes=meta["max_leaf_nodes"],
            min_samples_leaf=meta["min_samples_leaf"],
            l2_regularization=meta["l2_regularization"],
            seed=meta["seed"],
        )
        if meta.get("file") is None:
            if meta.get("constant") is None:
                raise ValueError("model metadata has neither a model file nor a constant")
            instance._constant = float(meta["constant"])
            return instance
        instance._model = _load_blob(path, meta)
        return instance


_REGRESSORS: dict[str, type[Regressor]] = {
    GradientBoostingRegressor.kind: GradientBoostingRegressor,
}


def regressor_kinds() -> list[str]:
    """The regressor kinds :func:`make_regressor` builds."""
    return sorted(_REGRESSORS)


def make_regressor(kind: str, **hyperparameters: Any) -> Regressor:
    """A new regressor of ``kind`` built with ``hyperparameters``."""
    if kind not in _REGRESSORS:
        raise ValueError(f"unknown regressor {kind!r}; choose one of {regressor_kinds()}")
    return _REGRESSORS[kind](**hyperparameters)


def load_regressor(path: Path) -> Regressor:
    """The regressor saved in directory ``path``, whatever its kind."""
    meta = json.loads((Path(path) / _META_FILE).read_text(encoding="utf-8"))
    kind = meta.get("kind")
    if kind not in _REGRESSORS:
        raise ValueError(f"unknown regressor {kind!r} in {path}")
    return _REGRESSORS[kind].load(Path(path))


# ---- bet sizing ---------------------------------------------------------------


def bet_size(p: float | np.ndarray, n_classes: int = 2, step: float | None = 0.1) -> Any:
    """Signed bet size in ``[-1, 1]`` for probability ``p`` of the
    predicted class (see the module doc): 0 at ``p = 1/K``, rounded to
    multiples of ``step`` (``None`` keeps it continuous). Scalar in,
    float out; array in, array out."""
    if n_classes < 2:
        raise ValueError(f"n_classes must be >= 2, got {n_classes}")
    if step is not None and not step > 0:
        raise ValueError(f"step must be positive, got {step}")
    prob = np.asarray(p, dtype=float)
    if np.any((prob < 0) | (prob > 1)):
        raise ValueError("probabilities must lie in [0, 1]")
    centre = 1.0 / n_classes
    with np.errstate(divide="ignore", invalid="ignore"):
        z = (prob - centre) / np.sqrt(prob * (1.0 - prob))
    z = np.where(prob == centre, 0.0, z)
    size = 2.0 * norm.cdf(z) - 1.0
    if step is not None:
        size = np.round(size / step) * step
    size = np.clip(size, -1.0, 1.0) + 0.0  # + 0.0 turns -0.0 into 0.0
    return float(size) if size.ndim == 0 else size


def break_even_probability(tp: float, sl: float) -> float:
    """``P(win)`` at which a ``tp`` take profit and an ``sl`` stop (same
    units) break even: ``sl / (tp + sl)``."""
    if not (tp > 0 and sl > 0):
        raise ValueError("tp and sl must be positive")
    return float(sl) / (float(tp) + float(sl))


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

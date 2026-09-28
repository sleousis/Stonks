"""Calibrated probabilities and conformal abstention before bet sizing
(roadmap 23.10, López de Prado *AFML* ch. 10, Vovk et al. on conformal
prediction).

A forest's ``P(win)`` is a vote share, not a probability: bet sizing reads
it as one, so an overconfident model bets too big. The fix has three parts,
all fitted on **purged out-of-fold** predictions of the training window
(:func:`purged_oof_proba`), never on the rows the model trained on:

- a :class:`Calibrator` maps raw scores to probabilities. ``isotonic``
  (a monotone step function) and ``platt`` (a logistic curve on the
  score's log odds) are registered, ``none`` keeps the raw score;
- a :class:`ConformalAbstainer` turns a score into a prediction set with
  ``1 - alpha`` coverage. When the set holds both classes (or none) the
  prediction is ambiguous and the strategy does not trade;
- :class:`ProbabilityPolicy` is the small seam a strategy holds: it applies
  the abstainer to the raw score, calibrates, and sizes the bet with
  :func:`stonks.features.ml.bet_size`, zero where it abstains.

scikit-learn does the fitting, but its types stay in this module: every
fitted object is plain numbers and saves as JSON (no pickle).

Research discipline: the calibration kind and ``alpha`` are strategy
parameters, so a search over them runs through the tuner and each value
tried is a trial in the ledger.
"""

from __future__ import annotations

import math
from abc import ABC, abstractmethod
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, ClassVar

import numpy as np

from stonks.features.ml import Classifier, bet_size

__all__ = [
    "Calibrator",
    "ConformalAbstainer",
    "IdentityCalibrator",
    "IsotonicCalibrator",
    "PlattCalibrator",
    "PolicyDecision",
    "ProbabilityPolicy",
    "calibrator_from_dict",
    "calibrator_kinds",
    "make_calibrator",
    "purged_oof_proba",
]

#: Log odds are taken of scores clipped to ``[_CLIP, 1 - _CLIP]``.
_CLIP = 1e-6


def _check(p: Any, y: Any, sample_weight: Any = None) -> tuple[np.ndarray, np.ndarray, Any]:
    prob = np.asarray(p, dtype=float).ravel()
    labels = np.asarray(y, dtype=int).ravel()
    if prob.shape != labels.shape or len(prob) == 0:
        raise ValueError("scores and labels must be non-empty and of equal length")
    if not np.all(np.isfinite(prob)) or np.any((prob < 0) | (prob > 1)):
        raise ValueError("scores must be finite and lie in [0, 1]")
    if not np.all(np.isin(labels, (0, 1))):
        raise ValueError("labels must be 0 or 1")
    weights = None if sample_weight is None else np.asarray(sample_weight, dtype=float).ravel()
    if weights is not None and weights.shape != prob.shape:
        raise ValueError("sample_weight must have one weight per score")
    return prob, labels, weights


# ---- calibrators ---------------------------------------------------------------


class Calibrator(ABC):
    """Maps raw classifier scores in ``[0, 1]`` to probabilities."""

    kind: ClassVar[str]

    @abstractmethod
    def fit(self, p: np.ndarray, y: np.ndarray, sample_weight: np.ndarray | None = None) -> None:
        """Fit on out-of-fold scores ``p`` and their 0/1 labels ``y``."""

    @abstractmethod
    def transform(self, p: np.ndarray) -> np.ndarray:
        """Calibrated probabilities, same shape as ``p``."""

    @abstractmethod
    def params(self) -> dict[str, Any]:
        """The fitted state as JSON-ready values."""

    @abstractmethod
    def load_params(self, params: dict[str, Any]) -> None: ...

    def to_dict(self) -> dict[str, Any]:
        return {"kind": self.kind, **self.params()}


_CALIBRATORS: dict[str, type[Calibrator]] = {}


def _register(cls: type[Calibrator]) -> type[Calibrator]:
    _CALIBRATORS[cls.kind] = cls
    return cls


def calibrator_kinds() -> list[str]:
    return sorted(_CALIBRATORS)


def make_calibrator(kind: str) -> Calibrator:
    try:
        return _CALIBRATORS[kind]()
    except KeyError:
        raise ValueError(f"unknown calibrator {kind!r}; choose from {calibrator_kinds()}") from None


def calibrator_from_dict(payload: dict[str, Any]) -> Calibrator:
    data = dict(payload)
    cal = make_calibrator(str(data.pop("kind")))
    cal.load_params(data)
    return cal


@_register
class IdentityCalibrator(Calibrator):
    """Keeps the raw score."""

    kind = "none"

    def fit(self, p: np.ndarray, y: np.ndarray, sample_weight: np.ndarray | None = None) -> None:
        _check(p, y, sample_weight)

    def transform(self, p: np.ndarray) -> np.ndarray:
        return np.asarray(p, dtype=float)

    def params(self) -> dict[str, Any]:
        return {}

    def load_params(self, params: dict[str, Any]) -> None:
        return None


@_register
class IsotonicCalibrator(Calibrator):
    """A monotone step function (scikit-learn ``IsotonicRegression``),
    stored as its knots and read back with linear interpolation. Scores
    outside the fitted range take the nearest end value."""

    kind = "isotonic"

    def __init__(self) -> None:
        self._x: np.ndarray | None = None
        self._y: np.ndarray | None = None

    def fit(self, p: np.ndarray, y: np.ndarray, sample_weight: np.ndarray | None = None) -> None:
        from sklearn.isotonic import IsotonicRegression

        prob, labels, weights = _check(p, y, sample_weight)
        model = IsotonicRegression(y_min=0.0, y_max=1.0, out_of_bounds="clip")
        model.fit(prob, labels.astype(float), sample_weight=weights)
        self._x = np.asarray(model.X_thresholds_, dtype=float).copy()
        self._y = np.asarray(model.y_thresholds_, dtype=float).copy()

    def transform(self, p: np.ndarray) -> np.ndarray:
        if self._x is None or self._y is None:
            raise RuntimeError("calibrator is not fitted")
        prob = np.asarray(p, dtype=float)
        return np.clip(np.interp(prob, self._x, self._y), 0.0, 1.0)

    def params(self) -> dict[str, Any]:
        if self._x is None or self._y is None:
            raise RuntimeError("calibrator is not fitted")
        return {"x": self._x.tolist(), "y": self._y.tolist()}

    def load_params(self, params: dict[str, Any]) -> None:
        self._x = np.asarray(params["x"], dtype=float)
        self._y = np.asarray(params["y"], dtype=float)


def _logit(p: np.ndarray) -> np.ndarray:
    q = np.clip(np.asarray(p, dtype=float), _CLIP, 1.0 - _CLIP)
    return np.log(q / (1.0 - q))


@_register
class PlattCalibrator(Calibrator):
    """``sigmoid(a * logit(p) + b)`` fitted by logistic regression
    (scikit-learn ``LogisticRegression``, almost no penalty)."""

    kind = "platt"

    def __init__(self) -> None:
        self._a: float | None = None
        self._b: float | None = None

    def fit(self, p: np.ndarray, y: np.ndarray, sample_weight: np.ndarray | None = None) -> None:
        prob, labels, weights = _check(p, y, sample_weight)
        if len(np.unique(labels)) < 2:
            # one class only: the best constant is its frequency
            rate = float(labels.mean())
            self._a, self._b = 0.0, float(_logit(np.array([rate]))[0])
            return
        from sklearn.linear_model import LogisticRegression

        model = LogisticRegression(C=1e6, max_iter=1000)
        model.fit(_logit(prob).reshape(-1, 1), labels, sample_weight=weights)
        self._a = float(model.coef_[0, 0])
        self._b = float(np.asarray(model.intercept_).ravel()[0])

    def transform(self, p: np.ndarray) -> np.ndarray:
        if self._a is None or self._b is None:
            raise RuntimeError("calibrator is not fitted")
        z = self._a * _logit(p) + self._b
        return 1.0 / (1.0 + np.exp(-z))

    def params(self) -> dict[str, Any]:
        if self._a is None or self._b is None:
            raise RuntimeError("calibrator is not fitted")
        return {"a": self._a, "b": self._b}

    def load_params(self, params: dict[str, Any]) -> None:
        self._a = float(params["a"])
        self._b = float(params["b"])


# ---- conformal abstention ------------------------------------------------------


class ConformalAbstainer:
    """Split conformal prediction for a binary score.

    The nonconformity of a labelled row is ``1 - P(its label)``. ``q`` is
    the ``ceil((n + 1)(1 - alpha)) / n`` quantile of those scores over the
    ``n`` calibration rows (infinite when ``n`` is too small, so every set
    holds both classes). A new score's prediction set holds class 1 when
    ``1 - p <= q`` and class 0 when ``p <= q``. Anything but exactly one
    class is ambiguous: abstain."""

    def __init__(self, alpha: float = 0.1) -> None:
        if not 0.0 < alpha < 1.0:
            raise ValueError(f"alpha must lie in (0, 1), got {alpha}")
        self.alpha = float(alpha)
        self.q: float | None = None
        self.n_fit = 0

    def fit(self, p: np.ndarray, y: np.ndarray) -> None:
        prob, labels, _ = _check(p, y)
        scores = np.where(labels == 1, 1.0 - prob, prob)
        n = len(scores)
        rank = math.ceil((n + 1) * (1.0 - self.alpha))
        self.q = math.inf if rank > n else float(np.sort(scores)[rank - 1])
        self.n_fit = n

    def prediction_set(self, p: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
        """``(holds class 0, holds class 1)`` per score."""
        if self.q is None:
            raise RuntimeError("abstainer is not fitted")
        prob = np.asarray(p, dtype=float)
        return prob <= self.q, (1.0 - prob) <= self.q

    def abstain(self, p: np.ndarray) -> np.ndarray:
        has0, has1 = self.prediction_set(p)
        return has0 == has1

    def to_dict(self) -> dict[str, Any]:
        if self.q is None:
            raise RuntimeError("abstainer is not fitted")
        return {"alpha": self.alpha, "q": None if math.isinf(self.q) else self.q, "n": self.n_fit}

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> ConformalAbstainer:
        out = cls(float(payload["alpha"]))
        q = payload.get("q")
        out.q = math.inf if q is None else float(q)
        out.n_fit = int(payload.get("n", 0))
        return out


# ---- purged out-of-fold scores -------------------------------------------------


def purged_oof_proba(
    make_classifier: Callable[[], Classifier],
    x: np.ndarray,
    y: np.ndarray,
    t0: Any,
    t1: Any,
    *,
    folds: int = 5,
    embargo_pct: float = 0.0,
    sample_weight: np.ndarray | None = None,
) -> np.ndarray:
    """Each row's ``P(1)`` from a fresh classifier fitted on the other,
    purged folds (:class:`stonks.lab.cv.PurgedKFold`). NaN for a row whose
    fold had no training rows left. Rows must be sorted by ``t0``."""
    from stonks.lab.cv import PurgedKFold

    x = np.asarray(x, dtype=float)
    y = np.asarray(y, dtype=int)
    weights = None if sample_weight is None else np.asarray(sample_weight, dtype=float)
    oof = np.full(len(y), np.nan)
    for train, test in PurgedKFold(folds, embargo_pct=embargo_pct).split(t0, t1):
        if len(train) == 0:
            continue
        clf = make_classifier()
        clf.fit(x[train], y[train], sample_weight=None if weights is None else weights[train])
        oof[test] = clf.predict_proba(x[test])
    return oof


# ---- the policy seam -----------------------------------------------------------


@dataclass(frozen=True)
class PolicyDecision:
    #: Calibrated probability per score.
    probability: np.ndarray
    #: True where the conformal set is ambiguous: do not trade.
    abstain: np.ndarray


class ProbabilityPolicy:
    """What a model strategy does with a raw score before sizing (see the
    module doc). Build it with :meth:`fit` on out-of-fold scores."""

    def __init__(
        self, calibrator: Calibrator, abstainer: ConformalAbstainer | None = None, n_fit: int = 0
    ) -> None:
        self.calibrator = calibrator
        self.abstainer = abstainer
        self.n_fit = int(n_fit)

    @classmethod
    def fit(
        cls,
        kind: str,
        oof: np.ndarray,
        y: np.ndarray,
        *,
        conformal_alpha: float | None = None,
        sample_weight: np.ndarray | None = None,
    ) -> ProbabilityPolicy:
        """Fit the calibrator ``kind`` and (with ``conformal_alpha``) the
        abstainer on the scored out-of-fold rows (NaN rows are skipped)."""
        scores = np.asarray(oof, dtype=float)
        labels = np.asarray(y, dtype=int)
        keep = np.isfinite(scores)
        if not keep.any():
            raise ValueError("no out-of-fold scores to calibrate on")
        weights = None if sample_weight is None else np.asarray(sample_weight, dtype=float)[keep]
        calibrator = make_calibrator(kind)
        calibrator.fit(scores[keep], labels[keep], weights)
        abstainer = None
        if conformal_alpha:
            abstainer = ConformalAbstainer(conformal_alpha)
            abstainer.fit(scores[keep], labels[keep])
        return cls(calibrator, abstainer, n_fit=int(keep.sum()))

    def apply(self, p: np.ndarray) -> PolicyDecision:
        raw = np.asarray(p, dtype=float)
        abstain = (
            self.abstainer.abstain(raw)
            if self.abstainer is not None
            else np.zeros(raw.shape, dtype=bool)
        )
        return PolicyDecision(probability=self.calibrator.transform(raw), abstain=abstain)

    def bet_size(self, p: np.ndarray, step: float | None = 0.1) -> np.ndarray:
        """:func:`~stonks.features.ml.bet_size` of the calibrated
        probability, zero where the policy abstains."""
        decision = self.apply(p)
        sizes = np.asarray(bet_size(decision.probability, step=step), dtype=float)
        return np.where(decision.abstain, 0.0, sizes)

    def to_dict(self) -> dict[str, Any]:
        return {
            "calibrator": self.calibrator.to_dict(),
            "conformal": None if self.abstainer is None else self.abstainer.to_dict(),
            "n_fit": self.n_fit,
        }

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> ProbabilityPolicy:
        conformal = payload.get("conformal")
        return cls(
            calibrator_from_dict(payload["calibrator"]),
            None if conformal is None else ConformalAbstainer.from_dict(conformal),
            n_fit=int(payload.get("n_fit", 0)),
        )

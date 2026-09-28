"""The feature schema check and the training feature profile of a model
(roadmap 23.10).

A model trained on features ``(a, b, c)`` reads a row as three numbers in
that order. If the code that builds the row changes (a feature renamed,
added, dropped or moved), the model silently reads the wrong numbers.
:func:`check_feature_schema` refuses that at model load: names and order
must match exactly.

:class:`FeatureProfile` is the training distribution of each feature,
stored with the model (``feature_profile.json`` in its artifact folder, so
each model version keeps its own). Bin edges are training quantiles, and
:meth:`FeatureProfile.psi` scores live values against them with the
population stability index (PSI): below 0.1 is stable, above 0.25 is a
real shift. The ``feature_drift`` tick hook reads it (warn only).
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import numpy as np

__all__ = [
    "PROFILE_FILE",
    "FeatureProfile",
    "FeatureSchemaError",
    "check_feature_schema",
]

PROFILE_FILE = "feature_profile.json"

#: Floor on a bin's share so an empty bin does not make PSI infinite.
_FLOOR = 1e-6


class FeatureSchemaError(ValueError):
    """The features a model was trained on differ from the ones it is fed."""


def check_feature_schema(expected: Sequence[str], actual: Sequence[str]) -> None:
    """Raise :class:`FeatureSchemaError` unless ``actual`` holds exactly the
    names of ``expected`` in the same order."""
    want, got = list(expected), list(actual)
    if want == got:
        return
    missing = [n for n in want if n not in got]
    extra = [n for n in got if n not in want]
    parts = []
    if missing:
        parts.append(f"missing {missing}")
    if extra:
        parts.append(f"unexpected {extra}")
    if not parts:
        parts.append(f"order differs: trained on {want}, got {got}")
    raise FeatureSchemaError("feature schema mismatch: " + "; ".join(parts))


def _psi(expected: np.ndarray, actual: np.ndarray) -> float:
    e = np.maximum(expected, _FLOOR)
    a = np.maximum(actual, _FLOOR)
    return float(np.sum((a - e) * np.log(a / e)))


@dataclass(frozen=True)
class FeatureProfile:
    """Per feature, inner bin edges and the training share of each bin."""

    names: tuple[str, ...]
    edges: tuple[tuple[float, ...], ...]
    shares: tuple[tuple[float, ...], ...]
    n_train: int

    @classmethod
    def build(cls, x: np.ndarray, names: Sequence[str], bins: int = 5) -> FeatureProfile:
        """Profile the training matrix ``x`` (one column per name). Non-finite
        values are skipped."""
        if bins < 2:
            raise ValueError(f"bins must be >= 2, got {bins}")
        matrix = np.atleast_2d(np.asarray(x, dtype=float))
        if matrix.shape[1] != len(names):
            raise FeatureSchemaError(
                f"training matrix has {matrix.shape[1]} columns for {len(names)} names"
            )
        edges: list[tuple[float, ...]] = []
        shares: list[tuple[float, ...]] = []
        for j in range(len(names)):
            col = matrix[:, j]
            col = col[np.isfinite(col)]
            if len(col) == 0:
                edges.append(())
                shares.append((1.0,))
                continue
            inner = np.unique(np.quantile(col, np.arange(1, bins) / bins))
            edges.append(tuple(float(v) for v in inner))
            shares.append(tuple(float(v) for v in _shares(col, inner)))
        return cls(tuple(names), tuple(edges), tuple(shares), int(len(matrix)))

    @property
    def profile_id(self) -> str:
        """A short digest of the profile: a new fit gives a new id."""
        text = json.dumps(self.to_dict(), sort_keys=True)
        return hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]

    def psi(self, x: np.ndarray, min_values: int = 1) -> dict[str, float]:
        """PSI per feature of the live matrix ``x`` (columns in profile
        order). A feature with fewer than ``min_values`` finite live values
        is left out."""
        matrix = np.atleast_2d(np.asarray(x, dtype=float))
        if matrix.shape[1] != len(self.names):
            raise FeatureSchemaError(
                f"live matrix has {matrix.shape[1]} columns for {len(self.names)} features"
            )
        out: dict[str, float] = {}
        for j, name in enumerate(self.names):
            col = matrix[:, j]
            col = col[np.isfinite(col)]
            if len(col) < max(1, min_values):
                continue
            inner = np.asarray(self.edges[j], dtype=float)
            out[name] = _psi(np.asarray(self.shares[j]), _shares(col, inner))
        return out

    def psi_rows(self, rows: Sequence[Mapping[str, Any]], min_values: int = 1) -> dict[str, float]:
        """:meth:`psi` of rows given as name to value mappings (a missing
        name counts as NaN)."""
        matrix = np.array(
            [[_number(row.get(name)) for name in self.names] for row in rows], dtype=float
        ).reshape(len(rows), len(self.names))
        return self.psi(matrix, min_values=min_values)

    # ---- persistence -------------------------------------------------------

    def to_dict(self) -> dict[str, Any]:
        return {
            "names": list(self.names),
            "edges": [list(e) for e in self.edges],
            "shares": [list(s) for s in self.shares],
            "n_train": self.n_train,
        }

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> FeatureProfile:
        return cls(
            tuple(str(n) for n in payload["names"]),
            tuple(tuple(float(v) for v in e) for e in payload["edges"]),
            tuple(tuple(float(v) for v in s) for s in payload["shares"]),
            int(payload.get("n_train", 0)),
        )

    def save(self, path: Path) -> None:
        path = Path(path)
        path.mkdir(parents=True, exist_ok=True)
        (path / PROFILE_FILE).write_text(
            json.dumps(self.to_dict(), indent=2, sort_keys=True), encoding="utf-8"
        )

    @classmethod
    def load(cls, path: Path) -> FeatureProfile:
        return cls.from_dict(json.loads((Path(path) / PROFILE_FILE).read_text(encoding="utf-8")))

    @classmethod
    def load_if_present(cls, path: Path) -> FeatureProfile | None:
        return cls.load(path) if (Path(path) / PROFILE_FILE).exists() else None


def _shares(values: np.ndarray, inner: np.ndarray) -> np.ndarray:
    idx = np.searchsorted(inner, values, side="right")
    counts = np.bincount(idx, minlength=len(inner) + 1).astype(float)
    return counts / counts.sum()


def _number(value: Any) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return float("nan")

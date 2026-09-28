"""Live feature drift of model strategies (roadmap 23.10).

A model strategy exposes two methods (duck-typed, see
:class:`ModelFeatureSource`):

- ``feature_profile()``: the training distribution of each feature, stored
  with the model version (:class:`~stonks.features.feature_profile.FeatureProfile`);
- ``model_feature_row(ticker, as_of, lake)``: the row the model scores
  today, feature name to value in training order, or ``None``.

Each real tick, :func:`check_feature_drift` records today's rows in
``model_feature_values`` and, once the trailing ``window_days`` days hold
``min_rows`` rows, scores them against the profile with PSI per feature.
A feature above ``warn_psi`` makes the finding ``warn``. Rows whose names
or order differ from the profile are a ``schema`` finding and are not
stored. Nothing here blocks a trade: findings are logged and land in the
tick summary.

Settings live under ``[production.feature_drift]``.
"""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from typing import Any, Literal, Protocol, runtime_checkable

from stonks.features.feature_profile import (
    FeatureProfile,
    FeatureSchemaError,
    check_feature_schema,
)
from stonks.logging import get_logger
from stonks.production.feature_drift_settings import FeatureDriftSettings
from stonks.store.state import SqliteState

__all__ = [
    "DriftFinding",
    "FeatureDriftSettings",
    "ModelFeatureSource",
    "check_feature_drift",
    "recent_feature_rows",
]

_log = get_logger("stonks.production.feature_drift")

FindingStatus = Literal["ok", "warn", "schema", "error"]


@runtime_checkable
class ModelFeatureSource(Protocol):
    def feature_profile(self) -> FeatureProfile | None: ...

    def model_feature_row(
        self, ticker: str, as_of: Any, lake: Any
    ) -> Mapping[str, float] | None: ...


@dataclass(frozen=True)
class DriftFinding:
    strategy_id: str
    status: FindingStatus
    profile_id: str | None = None
    rows: int = 0
    psi: Mapping[str, float] = field(default_factory=dict)
    detail: str = ""

    @property
    def max_psi(self) -> float:
        return max(self.psi.values(), default=0.0)

    @property
    def worst_feature(self) -> str | None:
        return max(self.psi, key=lambda k: self.psi[k]) if self.psi else None

    def as_dict(self) -> dict[str, Any]:
        return {
            "strategy_id": self.strategy_id,
            "status": self.status,
            "profile_id": self.profile_id,
            "rows": self.rows,
            "max_psi": self.max_psi,
            "worst_feature": self.worst_feature,
            "psi": dict(self.psi),
            "detail": self.detail,
        }


# ---- storage -----------------------------------------------------------------------


def _record(
    state: SqliteState,
    strategy_id: str,
    profile_id: str,
    as_of: date,
    rows: Sequence[tuple[str, Mapping[str, float]]],
) -> None:
    now = datetime.now(UTC).isoformat()
    with state.transaction():
        state.execute(
            "DELETE FROM model_feature_values WHERE strategy_id = ? AND profile_id = ?"
            " AND as_of = ?",
            [strategy_id, profile_id, as_of.isoformat()],
        )
        for ticker, values in rows:
            state.execute(
                "INSERT INTO model_feature_values"
                " (strategy_id, profile_id, as_of, ticker, values_json, recorded_at)"
                " VALUES (?, ?, ?, ?, ?, ?)",
                [strategy_id, profile_id, as_of.isoformat(), ticker, json.dumps(values), now],
            )


def recent_feature_rows(
    state: SqliteState, strategy_id: str, profile_id: str, as_of: date, window_days: int
) -> list[dict[str, float]]:
    """The live rows of the last ``window_days`` recorded days up to
    ``as_of``, oldest first."""
    found = state.sql(
        """
        SELECT values_json FROM model_feature_values
         WHERE strategy_id = ? AND profile_id = ? AND as_of IN (
               SELECT DISTINCT as_of FROM model_feature_values
                WHERE strategy_id = ? AND profile_id = ? AND as_of <= ?
                ORDER BY as_of DESC LIMIT ?)
         ORDER BY as_of, ticker
        """,
        [strategy_id, profile_id, strategy_id, profile_id, as_of.isoformat(), int(window_days)],
    )
    return [json.loads(r["values_json"]) for r in found]


# ---- the check ---------------------------------------------------------------------


def _profile_of(strategy: Any) -> FeatureProfile | None:
    getter = getattr(strategy, "feature_profile", None)
    if not callable(getter) or not callable(getattr(strategy, "model_feature_row", None)):
        return None
    profile = getter()
    return profile if isinstance(profile, FeatureProfile) else None


def _check_one(
    state: SqliteState,
    lake: Any,
    as_of: date,
    strategy_id: str,
    strategy: Any,
    profile: FeatureProfile,
    universe: Sequence[str],
    settings: FeatureDriftSettings,
) -> DriftFinding | None:
    pid = profile.profile_id
    rows: list[tuple[str, Mapping[str, float]]] = []
    for ticker in universe:
        row = strategy.model_feature_row(ticker, as_of, lake)
        if row is None:
            continue
        try:
            check_feature_schema(profile.names, list(row))
        except FeatureSchemaError as exc:
            return DriftFinding(strategy_id, "schema", pid, detail=str(exc))
        rows.append((ticker, {k: float(v) for k, v in row.items()}))
    if rows:
        _record(state, strategy_id, pid, as_of, rows)
    live = recent_feature_rows(state, strategy_id, pid, as_of, settings.window_days)
    if len(live) < settings.min_rows:
        return None
    psi = profile.psi_rows(live, min_values=settings.min_rows)
    worst = max(psi.values(), default=0.0)
    status: FindingStatus = "warn" if worst > settings.warn_psi else "ok"
    return DriftFinding(strategy_id, status, pid, rows=len(live), psi=psi)


def check_feature_drift(
    state: SqliteState,
    lake: Any,
    as_of: date,
    instances: Mapping[str, Any],
    universe: Sequence[str],
    settings: FeatureDriftSettings | None = None,
) -> list[DriftFinding]:
    """Record today's live rows of every model strategy in ``instances``
    and score the ones with a full window (see the module doc). A strategy
    that raises becomes an ``error`` finding; the others still run."""
    cfg = settings or FeatureDriftSettings()
    if not cfg.enabled:
        return []
    findings: list[DriftFinding] = []
    for strategy_id, strategy in instances.items():
        try:
            profile = _profile_of(strategy)
            if profile is None:
                continue
            finding = _check_one(state, lake, as_of, strategy_id, strategy, profile, universe, cfg)
        except Exception as exc:
            finding = DriftFinding(strategy_id, "error", detail=f"{type(exc).__name__}: {exc}")
        if finding is None:
            continue
        if finding.status != "ok":
            _log.warning("feature_drift.warn", as_of=as_of.isoformat(), **finding.as_dict())
        findings.append(finding)
    return findings

"""StrategyRegistry — SQLite metadata + on-disk artifact bundles.

Registers freshly-lab'd strategies in ``shadow`` status by default. A human
(or a soak-time policy) promotes them to ``active``; drift or bad live
performance can flip them back to ``shadow`` or all the way to ``retired``.
"""

from __future__ import annotations

import importlib
import json
import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from stonks.core.protocols import Strategy, SurvivalReport
from stonks.registry.artifact import ArtifactBundle
from stonks.store.state import SqliteState

_ALLOWED_STATUSES = ("active", "shadow", "retired")


@dataclass
class StrategyHandle:
    id: str
    class_path: str
    params: dict[str, Any]
    artifact_path: Path
    status: str
    created_at: str
    updated_at: str


class StrategyRegistry:
    def __init__(self, state: SqliteState, artifacts_dir: Path) -> None:
        self._state = state
        self._artifacts_dir = Path(artifacts_dir)
        self._artifacts_dir.mkdir(parents=True, exist_ok=True)

    # ---- writes ------------------------------------------------------------

    def register(
        self,
        strategy: Strategy,
        reports: Sequence[SurvivalReport],
        strategy_id: str | None = None,
    ) -> str:
        sid = strategy_id or self._generate_id(strategy)
        class_path = f"{type(strategy).__module__}:{type(strategy).__name__}"
        params = dict(getattr(strategy, "params", {}))
        now = _iso_now()
        artifact_path = self._artifacts_dir / sid

        ArtifactBundle(
            path=artifact_path,
            class_path=class_path,
            params=params,
            reports=list(reports),
            created_at=now,
        ).save()

        with self._state.transaction():
            self._state.execute(
                """
                INSERT INTO strategies
                    (id, class_path, params_json, artifact_path, status, created_at, updated_at)
                VALUES (?, ?, ?, ?, 'shadow', ?, ?)
                """,
                [sid, class_path, json.dumps(params, sort_keys=True), str(artifact_path), now, now],
            )
            for r in reports:
                self._state.execute(
                    """
                    INSERT INTO survival_reports
                        (strategy_id, test_id, passed, metrics_json, notes, created_at)
                    VALUES (?, ?, ?, ?, ?, ?)
                    """,
                    [
                        sid,
                        r.test_id,
                        1 if r.passed else 0,
                        json.dumps(dict(r.metrics), sort_keys=True),
                        r.notes,
                        now,
                    ],
                )
        return sid

    def set_status(self, strategy_id: str, status: str) -> None:
        if status not in _ALLOWED_STATUSES:
            raise ValueError(f"status must be one of {_ALLOWED_STATUSES}, got {status!r}")
        self._state.execute(
            "UPDATE strategies SET status = ?, updated_at = ? WHERE id = ?",
            [status, _iso_now(), strategy_id],
        )

    # ---- reads -------------------------------------------------------------

    def load(self, strategy_id: str) -> Strategy:
        handle = self._get_handle(strategy_id)
        module_name, cls_name = handle.class_path.split(":", 1)
        module = importlib.import_module(module_name)
        cls = getattr(module, cls_name)
        return cls(handle.params)

    def list_active(self) -> list[StrategyHandle]:
        return self._query("SELECT * FROM strategies WHERE status='active' ORDER BY created_at")

    def list_all(self, status: str | None = None) -> list[StrategyHandle]:
        if status is None:
            return self._query("SELECT * FROM strategies ORDER BY created_at")
        if status not in _ALLOWED_STATUSES:
            raise ValueError(f"status must be one of {_ALLOWED_STATUSES}, got {status!r}")
        return self._query(
            "SELECT * FROM strategies WHERE status=? ORDER BY created_at",
            [status],
        )

    def get_reports(self, strategy_id: str) -> list[SurvivalReport]:
        rows = self._state.sql(
            "SELECT test_id, passed, metrics_json, notes FROM survival_reports "
            "WHERE strategy_id=? ORDER BY created_at",
            [strategy_id],
        )
        return [
            SurvivalReport(
                test_id=row["test_id"],
                passed=bool(row["passed"]),
                metrics=json.loads(row["metrics_json"]),
                notes=row["notes"] or "",
            )
            for row in rows
        ]

    # ---- internals ---------------------------------------------------------

    def _get_handle(self, strategy_id: str) -> StrategyHandle:
        rows = self._query("SELECT * FROM strategies WHERE id=?", [strategy_id])
        if not rows:
            raise KeyError(strategy_id)
        return rows[0]

    def _query(self, query: str, params: list | None = None) -> list[StrategyHandle]:
        return [
            StrategyHandle(
                id=row["id"],
                class_path=row["class_path"],
                params=json.loads(row["params_json"]),
                artifact_path=Path(row["artifact_path"]),
                status=row["status"],
                created_at=row["created_at"],
                updated_at=row["updated_at"],
            )
            for row in self._state.sql(query, params or [])
        ]

    def _generate_id(self, strategy: Strategy) -> str:
        suffix = uuid.uuid4().hex[:8]
        return f"{getattr(strategy, 'id', 'strategy')}_{suffix}"


def _iso_now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")

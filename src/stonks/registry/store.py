"""StrategyRegistry — SQLite metadata + on-disk artifact bundles.

Registers freshly-lab'd strategies in ``shadow`` status by default. A human
(or a soak-time policy) promotes them to ``active``; drift or bad live
performance can flip them back to ``shadow`` or all the way to ``retired``.

Governance (BL-24): :meth:`StrategyRegistry.set_status` is the only writer of
``strategies.status``. Every change writes one append-only ``status_changes``
row (actor, from, to, reason, override flag, go-live report) in the same
transaction. Moving to ``active`` needs a passing go-live report for that
strategy, or ``override=True`` with a reason of at least
``MIN_OVERRIDE_REASON_CHARS`` characters. Demotions (``shadow``, ``retired``)
are always allowed but need a reason.
"""

from __future__ import annotations

import dataclasses
import importlib
import json
import math
import uuid
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal, get_args

from stonks.core.protocols import Strategy, SurvivalReport
from stonks.registry.artifact import ArtifactBundle
from stonks.store.state import SqliteState

_ALLOWED_STATUSES = ("active", "shadow", "retired")

#: Minimum length (after trimming) of the reason for a promotion override.
MIN_OVERRIDE_REASON_CHARS = 20

InterventionKind = Literal["status", "risk_reset", "manual_order", "config"]
_INTERVENTION_KINDS: tuple[str, ...] = get_args(InterventionKind)


class GovernanceError(ValueError):
    """A status change or intervention broke a governance rule (missing
    actor, missing or short reason, ...)."""


class PromotionRefused(GovernanceError):
    """Promotion to ``active`` without a passing go-live report and without
    an override."""


@dataclass(frozen=True)
class StatusChange:
    """One row of the intervention log."""

    id: int
    strategy_id: str | None
    kind: str
    from_status: str | None
    to_status: str | None
    actor: str
    reason: str
    override: bool
    golive_passed: bool | None
    golive_report: dict[str, Any] | None
    created_at: str


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
    def __init__(
        self,
        state: SqliteState,
        artifacts_dir: Path,
        *,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        """``clock`` returns an aware datetime for every timestamp the
        registry writes (default: the system clock). Tests pin it (TT-06)."""
        self._state = state
        self._artifacts_dir = Path(artifacts_dir)
        self._artifacts_dir.mkdir(parents=True, exist_ok=True)
        self._clock = clock

    # ---- writes ------------------------------------------------------------

    def register(
        self,
        strategy: Strategy,
        reports: Sequence[SurvivalReport],
        strategy_id: str | None = None,
    ) -> str:
        sid = strategy_id or self._generate_id(strategy)
        # Check before writing any artifact file: a failed INSERT after the
        # write would otherwise overwrite the existing strategy's artifact.
        if self._state.sql("SELECT 1 FROM strategies WHERE id = ?", [sid]):
            raise ValueError(f"strategy id {sid!r} is already registered")
        class_path = f"{type(strategy).__module__}:{type(strategy).__name__}"
        params = dict(getattr(strategy, "params", {}))
        now = self._now_iso()
        artifact_path = self._artifacts_dir / sid

        # The strategy persists itself first (params + any fitted state such
        # as ``fitted_state.json``); the bundle then adds reports and merges
        # its registry metadata into ``meta.json`` without dropping the
        # strategy's own keys.
        save = getattr(strategy, "save", None)
        if callable(save):
            artifact_path.mkdir(parents=True, exist_ok=True)
            save(artifact_path)
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
                # Relative to the artifacts folder (TO-09), so a restore
                # into another data folder still finds the bundle.
                [sid, class_path, json.dumps(params, sort_keys=True), sid, now, now],
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

    def set_status(
        self,
        strategy_id: str,
        status: str,
        *,
        actor: str | None = None,
        reason: str | None = None,
        golive_report: Any = None,
        override: bool = False,
    ) -> StatusChange | None:
        """Change a strategy's status and log it; the only status writer.

        ``golive_report`` is duck-typed (``production.golive.GoLiveReport``):
        it needs ``passed`` and ``strategy_id`` and is stored as JSON. Setting
        the current status again changes nothing, logs nothing and returns
        ``None``. Raises ``KeyError`` for an unknown id, ``ValueError`` for an
        unknown status, :class:`PromotionRefused` / :class:`GovernanceError`
        when a rule is broken (nothing is written then).
        """
        if status not in _ALLOWED_STATUSES:
            raise ValueError(f"status must be one of {_ALLOWED_STATUSES}, got {status!r}")
        with self._state.transaction():
            current = self._get_handle(strategy_id).status
            if current == status:
                return None
            actor_ = _require_actor(actor)
            reason_, golive_passed = _check_transition(
                strategy_id, current, status, reason, golive_report, override
            )
            now = self._now_iso()
            # Log first: the ``strategies_status_audited`` trigger only lets
            # the UPDATE through when the latest log row matches it.
            change = self._log(
                strategy_id=strategy_id,
                kind="status",
                from_status=current,
                to_status=status,
                actor=actor_,
                reason=reason_,
                override=override,
                golive_passed=golive_passed,
                golive_report=_report_json(golive_report),
                created_at=now,
            )
            cur = self._state.execute(
                "UPDATE strategies SET status = ?, updated_at = ? WHERE id = ? AND status = ?",
                [status, now, strategy_id, current],
            )
            if cur.rowcount != 1:  # pragma: no cover - guarded by the transaction
                raise GovernanceError(f"strategy {strategy_id!r} changed status concurrently")
            if current == "active":
                # BE-01: auto trades only an active strategy. Its running auto
                # subscriptions pause in this same transaction. Imported here:
                # the registry sits below production.
                from stonks.production.auto_pause import pause_auto_for_strategy

                pause_auto_for_strategy(self._state, strategy_id, status)
            return change

    def record_intervention(
        self,
        kind: InterventionKind,
        *,
        actor: str,
        reason: str,
        strategy_id: str | None = None,
    ) -> StatusChange:
        """Log a non-status intervention (risk reset, manual order, config
        change). Status changes go through :meth:`set_status` only."""
        if kind == "status" or kind not in _INTERVENTION_KINDS:
            allowed = [k for k in _INTERVENTION_KINDS if k != "status"]
            raise GovernanceError(f"intervention kind must be one of {allowed}, got {kind!r}")
        with self._state.transaction():
            return self._log(
                strategy_id=strategy_id,
                kind=kind,
                from_status=None,
                to_status=None,
                actor=_require_actor(actor),
                reason=_require_reason(reason, what=kind),
                override=False,
                golive_passed=None,
                golive_report=None,
                created_at=self._now_iso(),
            )

    # ---- reads -------------------------------------------------------------

    def load(self, strategy_id: str) -> Strategy:
        handle = self._get_handle(strategy_id)
        module_name, cls_name = handle.class_path.split(":", 1)
        module = importlib.import_module(module_name)
        cls = getattr(module, cls_name)
        # Prefer the class's own loader so fitted state saved at register
        # time is restored; bare constructors only get the params.
        loader = getattr(cls, "load", None)
        if callable(loader):
            return loader(handle.artifact_path)
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

    def status_history(self, strategy_id: str) -> list[StatusChange]:
        """The strategy's logged changes and interventions, oldest first."""
        rows = self._state.sql(
            "SELECT * FROM status_changes WHERE strategy_id = ? ORDER BY id", [strategy_id]
        )
        return [_change_from_row(r) for r in rows]

    # ---- internals ---------------------------------------------------------

    def _log(self, **fields: Any) -> StatusChange:
        golive_passed = fields["golive_passed"]
        cur = self._state.execute(
            """
            INSERT INTO status_changes
                (strategy_id, kind, from_status, to_status, actor, reason, override,
                 golive_passed, golive_report_json, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            [
                fields["strategy_id"],
                fields["kind"],
                fields["from_status"],
                fields["to_status"],
                fields["actor"],
                fields["reason"],
                1 if fields["override"] else 0,
                None if golive_passed is None else int(golive_passed),
                fields["golive_report"],
                fields["created_at"],
            ],
        )
        (row,) = self._state.sql("SELECT * FROM status_changes WHERE id = ?", [cur.lastrowid])
        return _change_from_row(row)

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
                artifact_path=self.resolve_artifact_path(row["artifact_path"]),
                status=row["status"],
                created_at=row["created_at"],
                updated_at=row["updated_at"],
            )
            for row in self._state.sql(query, params or [])
        ]

    def resolve_artifact_path(self, stored: str) -> Path:
        """The bundle folder for a stored ``artifact_path`` (TO-09).

        New rows hold a path relative to the artifacts folder. Rows written
        before that hold an absolute path under the data folder of the
        time; those resolve to the same bundle name under the current
        artifacts folder when it exists there (after a restore into a new
        data folder), and to the stored path otherwise.
        """
        path = Path(stored)
        if not path.is_absolute():
            return self._artifacts_dir / path
        moved = self._artifacts_dir / path.name
        return moved if moved.is_dir() else path

    def _now_iso(self) -> str:
        if self._clock is None:
            return _iso_now()
        return self._clock().astimezone(UTC).isoformat(timespec="seconds")

    def _generate_id(self, strategy: Strategy) -> str:
        suffix = uuid.uuid4().hex[:8]
        return f"{getattr(strategy, 'id', 'strategy')}_{suffix}"


def _require_actor(actor: str | None) -> str:
    if not isinstance(actor, str) or not actor.strip():
        raise GovernanceError("an actor is required for every status change")
    return actor.strip()


def _require_reason(reason: str | None, *, what: str) -> str:
    if not isinstance(reason, str) or not reason.strip():
        raise GovernanceError(f"a reason is required for {what}")
    return reason.strip()


def _check_transition(
    strategy_id: str,
    current: str,
    status: str,
    reason: str | None,
    golive_report: Any,
    override: bool,
) -> tuple[str, bool | None]:
    """Apply the promotion rules; returns the reason to log and whether the
    go-live report passed (``None`` without one). A report only counts when
    it was evaluated for this strategy (it must name it) in its current
    status. A mapping report is read by its keys."""
    golive_passed = None if golive_report is None else bool(_report_field(golive_report, "passed"))
    if status != "active":
        return _require_reason(reason, what=f"moving a strategy to {status!r}"), golive_passed
    if golive_report is not None:
        report_sid = _report_field(golive_report, "strategy_id")
        report_status = _report_field(golive_report, "status", current)
        problem = None
        if report_sid is None:
            problem = f"go-live report names no strategy, so it cannot count for {strategy_id!r}"
        elif report_sid != strategy_id:
            problem = (
                f"go-live report is for another strategy ({report_sid!r}), not {strategy_id!r}"
            )
        elif report_status != current:
            problem = (
                f"go-live report is stale: evaluated while {report_status!r}, "
                f"strategy is now {current!r}"
            )
        if problem is not None:
            golive_passed = False
            if not override:
                raise PromotionRefused(problem)
    if override:
        text = (reason or "").strip()
        if len(text) < MIN_OVERRIDE_REASON_CHARS:
            raise GovernanceError(
                f"a promotion override needs a reason of at least "
                f"{MIN_OVERRIDE_REASON_CHARS} characters"
            )
        return text, golive_passed
    if golive_report is None:
        raise PromotionRefused(
            f"promoting {strategy_id!r} needs a passing go-live check "
            "(or an override with a reason)"
        )
    if not golive_passed:
        failed = [
            f"{_report_field(c, 'name', '?')}: {_report_field(c, 'detail', '')}".rstrip(": ")
            for c in _report_field(golive_report, "checks", None) or []
            if not _report_field(c, "passed", False)
        ]
        raise PromotionRefused(
            f"go-live check failed for {strategy_id!r}: " + ("; ".join(failed) or "no checks")
        )
    text = (reason or "").strip()
    return text or "go-live check passed", golive_passed


def _report_field(report: Any, name: str, default: Any = None) -> Any:
    """A go-live report's field: a mapping's key, else an attribute."""
    if isinstance(report, Mapping):
        return report.get(name, default)
    return getattr(report, name, default)


def _report_json(report: Any) -> str | None:
    if report is None:
        return None
    if dataclasses.is_dataclass(report) and not isinstance(report, type):
        payload: Any = dataclasses.asdict(report)
        payload["passed"] = bool(report.passed)
    elif isinstance(report, Mapping):
        payload = dict(report)
    else:
        to_dict = getattr(report, "to_dict", None) or getattr(report, "model_dump", None)
        payload = to_dict() if callable(to_dict) else {"passed": bool(report.passed)}
    return json.dumps(_finite(payload), sort_keys=True, default=str, allow_nan=False)


def _finite(value: Any) -> Any:
    """Strict JSON: non-finite floats become ``None``."""
    if isinstance(value, float):
        return value if math.isfinite(value) else None
    if isinstance(value, Mapping):
        return {k: _finite(v) for k, v in value.items()}
    if isinstance(value, list | tuple):
        return [_finite(v) for v in value]
    return value


def _change_from_row(row: Any) -> StatusChange:
    passed = row["golive_passed"]
    report = row["golive_report_json"]
    return StatusChange(
        id=row["id"],
        strategy_id=row["strategy_id"],
        kind=row["kind"],
        from_status=row["from_status"],
        to_status=row["to_status"],
        actor=row["actor"],
        reason=row["reason"],
        override=bool(row["override"]),
        golive_passed=None if passed is None else bool(passed),
        golive_report=json.loads(report) if report else None,
        created_at=row["created_at"],
    )


def _iso_now() -> str:
    """The system clock; golden tests patch this module function."""
    return datetime.now(UTC).isoformat(timespec="seconds")

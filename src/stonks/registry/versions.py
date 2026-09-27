"""Model versions under one strategy id (roadmap 22.6).

A registered strategy keeps its id for life, while the model inside it can
be fitted again. Each fit is a *version*:

- version 1, the ``baseline``, is the artifact the strategy was registered
  with. :meth:`ModelVersionRegistry.ensure_baseline` records it the first
  time versions are asked for;
- a retrain adds a ``candidate`` (its artifact under
  ``<artifacts>/<id>/versions/v<n>/``). A candidate never trades: the tick
  runs it as a model book beside the live version;
- :meth:`ModelVersionRegistry.swap` makes a candidate ``live`` and points
  ``strategies.artifact_path`` at it, so the next tick loads the new model.
  The old live version becomes ``archived``;
- a candidate can be ``rejected`` by hand, or superseded by a newer
  candidate. A fit that raised is kept as ``failed`` with its error.

Governance mirrors :meth:`StrategyRegistry.set_status`: this class is the
only writer of ``model_versions.status`` and of ``strategies.artifact_path``,
and every change writes one append-only ``model_version_events`` row first
(triggers refuse any other write). A swap needs a passing swap check for
that strategy and version (duck-typed: ``passed``, ``strategy_id``,
``version``), or ``override=True`` with a reason of at least
:data:`~stonks.registry.store.MIN_OVERRIDE_REASON_CHARS` characters. A
retired strategy never swaps.
"""

from __future__ import annotations

import importlib
import json
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any, Literal, cast

from stonks.core.protocols import Strategy
from stonks.registry.store import (
    MIN_OVERRIDE_REASON_CHARS,
    GovernanceError,
    StrategyRegistry,
    _finite,
)
from stonks.store.state import SqliteState

VersionStatus = Literal["live", "candidate", "archived", "rejected", "failed"]
EventKind = Literal["baseline", "candidate", "swap", "reject", "supersede", "fail"]

#: The folder under a strategy's bundle that holds its later versions.
VERSIONS_DIR = "versions"

#: The actor of rows the system writes on its own (the baseline).
SYSTEM_ACTOR = "system"


class SwapRefused(GovernanceError):
    """A swap without a passing check for this version and without an override."""


@dataclass(frozen=True)
class ModelVersion:
    strategy_id: str
    version: int
    artifact_path: Path
    status: VersionStatus
    train_start: date | None
    train_end: date | None
    fit: dict[str, Any]
    error: str | None
    created_by: str
    created_at: str
    updated_at: str

    @property
    def book_id(self) -> str:
        """The id of this version's model book (``<strategy>@v<n>``)."""
        return book_id(self.strategy_id, self.version)


@dataclass(frozen=True)
class VersionEvent:
    id: int
    strategy_id: str
    version: int
    kind: EventKind
    from_status: str | None
    to_status: str
    actor: str
    reason: str
    override: bool
    check_passed: bool | None
    check_report: dict[str, Any] | None
    created_at: str


def book_id(strategy_id: str, version: int) -> str:
    return f"{strategy_id}@v{version}"


def parse_book_id(value: str) -> tuple[str, int] | None:
    """``(strategy_id, version)`` of a version book id, else ``None``."""
    sid, sep, ver = value.rpartition("@v")
    if not sep or not sid or not ver.isdigit():
        return None
    return sid, int(ver)


class ModelVersionRegistry:
    def __init__(
        self,
        state: SqliteState,
        artifacts_dir: Path,
        *,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._state = state
        self._artifacts_dir = Path(artifacts_dir)
        self._clock = clock
        self._strategies = StrategyRegistry(state, self._artifacts_dir, clock=clock)

    @classmethod
    def on(cls, registry: StrategyRegistry) -> ModelVersionRegistry:
        """The version registry over the same state and artifacts as ``registry``."""
        return cls(registry._state, registry._artifacts_dir, clock=registry._clock)

    # ---- reads -------------------------------------------------------------

    def list(self, strategy_id: str) -> list[ModelVersion]:
        """Every version of the strategy, oldest first (the baseline is
        recorded on the first call). ``KeyError`` for an unknown id."""
        self.ensure_baseline(strategy_id)
        rows = self._state.sql(
            "SELECT * FROM model_versions WHERE strategy_id = ? ORDER BY version", [strategy_id]
        )
        return [self._from_row(r) for r in rows]

    def get(self, strategy_id: str, version: int) -> ModelVersion:
        self.ensure_baseline(strategy_id)
        rows = self._state.sql(
            "SELECT * FROM model_versions WHERE strategy_id = ? AND version = ?",
            [strategy_id, int(version)],
        )
        if not rows:
            raise KeyError(f"{strategy_id} v{version}")
        return self._from_row(rows[0])

    def live(self, strategy_id: str) -> ModelVersion:
        return next(v for v in self.list(strategy_id) if v.status == "live")

    def candidates(self, strategy_id: str | None = None) -> list[ModelVersion]:
        """Candidate versions (of one strategy, or of every strategy that is
        not retired), by strategy then version."""
        query = (
            "SELECT mv.* FROM model_versions mv JOIN strategies s ON s.id = mv.strategy_id"
            " WHERE mv.status = 'candidate' AND s.status <> 'retired'"
        )
        params: list[Any] = []
        if strategy_id is not None:
            query += " AND mv.strategy_id = ?"
            params.append(strategy_id)
        query += " ORDER BY s.created_at, mv.strategy_id, mv.version"
        return [self._from_row(r) for r in self._state.sql(query, params)]

    def history(self, strategy_id: str) -> list[VersionEvent]:
        """The strategy's version events, oldest first."""
        rows = self._state.sql(
            "SELECT * FROM model_version_events WHERE strategy_id = ? ORDER BY id", [strategy_id]
        )
        return [_event_from_row(r) for r in rows]

    def load(self, strategy_id: str, version: int) -> Strategy:
        """The strategy with this version's fitted state."""
        handle = self._strategies._get_handle(strategy_id)
        target = self.get(strategy_id, version)
        module_name, cls_name = handle.class_path.split(":", 1)
        cls = getattr(importlib.import_module(module_name), cls_name)
        loader = getattr(cls, "load", None)
        loaded = loader(target.artifact_path) if callable(loader) else cls(handle.params)
        return cast(Strategy, loaded)

    def next_version(self, strategy_id: str) -> tuple[int, str, Path]:
        """The next version number, its stored (relative) artifact path and
        its folder."""
        self.ensure_baseline(strategy_id)
        (row,) = self._state.sql(
            "SELECT COALESCE(MAX(version), 0) AS v FROM model_versions WHERE strategy_id = ?",
            [strategy_id],
        )
        version = int(row["v"]) + 1
        rel = f"{strategy_id}/{VERSIONS_DIR}/v{version}"
        return version, rel, self._artifacts_dir / rel

    # ---- writes ------------------------------------------------------------

    def ensure_baseline(self, strategy_id: str) -> None:
        """Record version 1 (the registered artifact) as live when the
        strategy has no versions yet. ``KeyError`` for an unknown id."""
        rows = self._state.sql(
            "SELECT artifact_path, created_at FROM strategies WHERE id = ?", [strategy_id]
        )
        if not rows:
            raise KeyError(strategy_id)
        if self._state.sql("SELECT 1 FROM model_versions WHERE strategy_id = ?", [strategy_id]):
            return
        now = self._now_iso()
        with self._state.transaction():
            if self._state.sql(
                "SELECT 1 FROM model_versions WHERE strategy_id = ?", [strategy_id]
            ):  # pragma: no cover - raced
                return
            self._insert(
                strategy_id,
                1,
                rows[0]["artifact_path"],
                "live",
                created_by=SYSTEM_ACTOR,
                created_at=rows[0]["created_at"] or now,
            )
            self._log(
                strategy_id, 1, "baseline", None, "live", SYSTEM_ACTOR, "registered fit", now=now
            )

    def add_candidate(
        self,
        strategy_id: str,
        version: int,
        artifact_path: str,
        *,
        train_start: date,
        train_end: date,
        fit: Mapping[str, Any] | None = None,
        actor: str,
        reason: str = "scheduled retrain",
    ) -> ModelVersion:
        """Record a fitted candidate whose artifact is already on disk at
        ``artifact_path`` (relative to the artifacts folder). Older
        candidates of the strategy are superseded in the same transaction."""
        actor_ = _require(actor, "an actor")
        now = self._now_iso()
        with self._state.transaction():
            self._require_strategy(strategy_id, allow_retired=False)
            for old in self._rows(strategy_id, "candidate"):
                self._log(
                    strategy_id,
                    old,
                    "supersede",
                    "candidate",
                    "rejected",
                    actor_,
                    f"superseded by v{version}",
                    now=now,
                )
                self._set(strategy_id, old, "rejected", now)
            self._insert(
                strategy_id,
                version,
                artifact_path,
                "candidate",
                created_by=actor_,
                created_at=now,
                train_start=train_start,
                train_end=train_end,
                fit=fit,
            )
            self._log(strategy_id, version, "candidate", None, "candidate", actor_, reason, now=now)
        return self.get(strategy_id, version)

    def record_failure(
        self,
        strategy_id: str,
        version: int,
        artifact_path: str,
        *,
        train_start: date,
        train_end: date,
        error: str,
        actor: str,
    ) -> ModelVersion:
        """Keep a fit that raised, with its error. It never trades."""
        actor_ = _require(actor, "an actor")
        now = self._now_iso()
        with self._state.transaction():
            self._require_strategy(strategy_id, allow_retired=True)
            self._insert(
                strategy_id,
                version,
                artifact_path,
                "failed",
                created_by=actor_,
                created_at=now,
                train_start=train_start,
                train_end=train_end,
                error=error,
            )
            self._log(
                strategy_id, version, "fail", None, "failed", actor_, error[:2000] or "fit failed",
                now=now,
            )  # fmt: skip
        return self.get(strategy_id, version)

    def swap(
        self,
        strategy_id: str,
        version: int,
        *,
        actor: str,
        reason: str | None = None,
        check_report: Any = None,
        override: bool = False,
    ) -> VersionEvent:
        """Make candidate ``version`` live; the only writer of a live
        version and of ``strategies.artifact_path``. Raises ``KeyError``
        for an unknown strategy or version, :class:`SwapRefused` without a
        passing check (or an override with a reason), and
        :class:`GovernanceError` for other broken rules."""
        actor_ = _require(actor, "an actor")
        self.ensure_baseline(strategy_id)
        with self._state.transaction():
            self._require_strategy(strategy_id, allow_retired=False)
            target = self.get(strategy_id, version)
            if target.status != "candidate":
                raise GovernanceError(
                    f"{strategy_id} v{version} is {target.status}, only a candidate can swap in"
                )
            reason_, passed = _check_swap(strategy_id, version, reason, check_report, override)
            current = self.live(strategy_id)
            now = self._now_iso()
            report_json = _report_json(check_report)
            self._log(
                strategy_id,
                current.version,
                "swap",
                "live",
                "archived",
                actor_,
                f"replaced by v{version}: {reason_}",
                now=now,
            )
            self._set(strategy_id, current.version, "archived", now)
            event = self._log(
                strategy_id,
                version,
                "swap",
                "candidate",
                "live",
                actor_,
                reason_,
                override=override,
                check_passed=passed,
                check_report=report_json,
                now=now,
            )
            self._set(strategy_id, version, "live", now)
            (row,) = self._state.sql(
                "SELECT artifact_path FROM model_versions WHERE strategy_id = ? AND version = ?",
                [strategy_id, version],
            )
            self._state.execute(
                "UPDATE strategies SET artifact_path = ?, updated_at = ? WHERE id = ?",
                [row["artifact_path"], now, strategy_id],
            )
        return event

    def reject(self, strategy_id: str, version: int, *, actor: str, reason: str) -> VersionEvent:
        """Drop a candidate: its model book stops and it can never swap in."""
        actor_ = _require(actor, "an actor")
        reason_ = _require(reason, "a reason")
        with self._state.transaction():
            target = self.get(strategy_id, version)
            if target.status != "candidate":
                raise GovernanceError(
                    f"{strategy_id} v{version} is {target.status}, only a candidate can be rejected"
                )
            now = self._now_iso()
            event = self._log(
                strategy_id, version, "reject", "candidate", "rejected", actor_, reason_, now=now
            )
            self._set(strategy_id, version, "rejected", now)
        return event

    # ---- internals ---------------------------------------------------------

    def _require_strategy(self, strategy_id: str, *, allow_retired: bool) -> None:
        rows = self._state.sql("SELECT status FROM strategies WHERE id = ?", [strategy_id])
        if not rows:
            raise KeyError(strategy_id)
        if not allow_retired and rows[0]["status"] == "retired":
            raise GovernanceError(f"strategy {strategy_id!r} is retired")

    def _rows(self, strategy_id: str, status: str) -> list[int]:
        rows = self._state.sql(
            "SELECT version FROM model_versions WHERE strategy_id = ? AND status = ?"
            " ORDER BY version",
            [strategy_id, status],
        )
        return [int(r["version"]) for r in rows]

    def _insert(
        self,
        strategy_id: str,
        version: int,
        artifact_path: str,
        status: str,
        *,
        created_by: str,
        created_at: str,
        train_start: date | None = None,
        train_end: date | None = None,
        fit: Mapping[str, Any] | None = None,
        error: str | None = None,
    ) -> None:
        self._state.execute(
            """
            INSERT INTO model_versions
                (strategy_id, version, artifact_path, status, train_start, train_end,
                 fit_json, error, created_by, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            [
                strategy_id,
                int(version),
                artifact_path,
                status,
                None if train_start is None else train_start.isoformat(),
                None if train_end is None else train_end.isoformat(),
                None if fit is None else _dumps(dict(fit)),
                error,
                created_by,
                created_at,
                created_at,
            ],
        )

    def _set(self, strategy_id: str, version: int, status: str, now: str) -> None:
        self._state.execute(
            "UPDATE model_versions SET status = ?, updated_at = ? WHERE strategy_id = ? AND version = ?",
            [status, now, strategy_id, int(version)],
        )

    def _log(
        self,
        strategy_id: str,
        version: int,
        kind: EventKind,
        from_status: str | None,
        to_status: str,
        actor: str,
        reason: str,
        *,
        now: str,
        override: bool = False,
        check_passed: bool | None = None,
        check_report: str | None = None,
    ) -> VersionEvent:
        cur = self._state.execute(
            """
            INSERT INTO model_version_events
                (strategy_id, version, kind, from_status, to_status, actor, reason, override,
                 check_passed, check_report_json, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            [
                strategy_id,
                int(version),
                kind,
                from_status,
                to_status,
                actor,
                reason,
                1 if override else 0,
                None if check_passed is None else int(check_passed),
                check_report,
                now,
            ],
        )
        (row,) = self._state.sql("SELECT * FROM model_version_events WHERE id = ?", [cur.lastrowid])
        return _event_from_row(row)

    def _from_row(self, row: Any) -> ModelVersion:
        fit = row["fit_json"]
        return ModelVersion(
            strategy_id=row["strategy_id"],
            version=int(row["version"]),
            artifact_path=self._strategies.resolve_artifact_path(row["artifact_path"]),
            status=row["status"],
            train_start=_date(row["train_start"]),
            train_end=_date(row["train_end"]),
            fit=json.loads(fit) if fit else {},
            error=row["error"],
            created_by=row["created_by"],
            created_at=row["created_at"],
            updated_at=row["updated_at"],
        )

    def _now_iso(self) -> str:
        now = self._clock() if self._clock is not None else datetime.now(UTC)
        return now.astimezone(UTC).isoformat(timespec="seconds")


def _check_swap(
    strategy_id: str, version: int, reason: str | None, report: Any, override: bool
) -> tuple[str, bool | None]:
    """The reason to log and whether the check passed (``None`` without a
    report). A report counts only for this strategy and version."""
    passed = None if report is None else bool(report.passed)
    if report is not None:
        sid = getattr(report, "strategy_id", strategy_id)
        ver = getattr(report, "version", version)
        if sid != strategy_id or int(ver) != int(version):
            passed = False
            if not override:
                raise SwapRefused(f"swap check is for {sid} v{ver}, not {strategy_id} v{version}")
    if override:
        text = (reason or "").strip()
        if len(text) < MIN_OVERRIDE_REASON_CHARS:
            raise GovernanceError(
                f"a swap override needs a reason of at least {MIN_OVERRIDE_REASON_CHARS} characters"
            )
        return text, passed
    if report is None:
        raise SwapRefused(
            f"swapping in {strategy_id} v{version} needs a passing swap check "
            "(or an override with a reason)"
        )
    if not passed:
        failed = [
            f"{getattr(c, 'name', '?')}: {getattr(c, 'detail', '')}".rstrip(": ")
            for c in getattr(report, "checks", [])
            if not getattr(c, "passed", False)
        ]
        raise SwapRefused(
            f"swap check failed for {strategy_id} v{version}: " + ("; ".join(failed) or "no checks")
        )
    return (reason or "").strip() or "swap check passed", passed


def _report_json(report: Any) -> str | None:
    if report is None:
        return None
    to_dict = getattr(report, "as_dict", None)
    payload = to_dict() if callable(to_dict) else {"passed": bool(report.passed)}
    return _dumps(payload)


def _dumps(payload: Any) -> str:
    return json.dumps(_finite(payload), sort_keys=True, default=str, allow_nan=False)


def _require(value: str | None, what: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise GovernanceError(f"{what} is required for every version change")
    return value.strip()


def _date(value: str | None) -> date | None:
    return date.fromisoformat(value) if value else None


def _event_from_row(row: Any) -> VersionEvent:
    passed = row["check_passed"]
    report = row["check_report_json"]
    return VersionEvent(
        id=int(row["id"]),
        strategy_id=row["strategy_id"],
        version=int(row["version"]),
        kind=row["kind"],
        from_status=row["from_status"],
        to_status=row["to_status"],
        actor=row["actor"],
        reason=row["reason"],
        override=bool(row["override"]),
        check_passed=None if passed is None else bool(passed),
        check_report=json.loads(report) if report else None,
        created_at=row["created_at"],
    )

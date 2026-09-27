"""The live stage of a portfolio (roadmap 19.9, design section 1).

Every portfolio stands on one stage on the way to real money::

    sim_paper -> broker_paper -> live_small -> live_scale

- A **promotion** moves exactly one stage up. It needs a gate report that
  passed, computed at that moment (``production.live.gates``). Who may
  promote (the owner, with a fresh second factor and a typed confirmation)
  is checked by the service layer before it calls :func:`change_stage`.
- A **demotion** moves any number of stages down with a reason. It needs no
  report: moving down only reduces risk (P28, P40).

Every change writes a ``live_stage_changes`` row first (append-only) and an
``audit_log`` row, then moves ``portfolios.live_stage``. A trigger refuses
any stage write without its log row. There is no automatic ramp: a dirty
week raises an alert and never changes the stage.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Literal, cast, get_args

from stonks.accounts.audit import AuditLog
from stonks.core.clock import SYSTEM_CLOCK, Clock, iso_now
from stonks.store.state import SqliteState

LiveStage = Literal["sim_paper", "broker_paper", "live_small", "live_scale"]
Direction = Literal["promote", "demote"]

#: The stages in order, lowest first.
STAGES: tuple[LiveStage, ...] = get_args(LiveStage)
#: The stages that trade real money.
REAL_MONEY: frozenset[LiveStage] = frozenset({"live_small", "live_scale"})
DEFAULT_STAGE: LiveStage = "sim_paper"

TABLE = "live_stage_changes"


class StageError(ValueError):
    """A stage change that breaks a rule (skips a stage, no report, ...)."""


@dataclass(frozen=True)
class StageChange:
    id: int
    portfolio_id: str
    from_stage: LiveStage
    to_stage: LiveStage
    direction: Direction
    actor: str
    reason: str
    gate_report: dict[str, Any] | None
    created_at: str


def stages_enabled(state: SqliteState) -> bool:
    """The state DB carries the stage tables (migration 034)."""
    rows = state.sql("SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?", [TABLE])
    return bool(rows)


def stage_index(stage: str) -> int:
    if stage not in STAGES:
        raise StageError(f"unknown stage {stage!r}; stages are {', '.join(STAGES)}")
    return STAGES.index(cast(LiveStage, stage))


def next_stage(stage: LiveStage) -> LiveStage | None:
    """The stage a promotion leads to (``None`` from the top)."""
    i = stage_index(stage)
    return STAGES[i + 1] if i + 1 < len(STAGES) else None


def trades_real_money(stage: LiveStage) -> bool:
    return stage in REAL_MONEY


def get_stage(state: SqliteState, portfolio_id: str) -> LiveStage:
    """The portfolio's stage (``sim_paper`` for an unknown portfolio or a
    state DB before migration 034)."""
    if not stages_enabled(state):
        return DEFAULT_STAGE
    rows = state.sql("SELECT live_stage FROM portfolios WHERE id = ?", [portfolio_id])
    if not rows or rows[0]["live_stage"] not in STAGES:
        return DEFAULT_STAGE
    return cast(LiveStage, rows[0]["live_stage"])


def stage_history(state: SqliteState, portfolio_id: str, limit: int = 100) -> list[StageChange]:
    """The portfolio's stage changes, newest first."""
    if not stages_enabled(state):
        return []
    rows = state.sql(
        f"SELECT * FROM {TABLE} WHERE portfolio_id = ? ORDER BY id DESC LIMIT ?",
        [portfolio_id, limit],
    )
    return [_change(r) for r in rows]


def change_stage(
    state: SqliteState,
    portfolio_id: str,
    to_stage: LiveStage,
    *,
    actor: str,
    reason: str,
    gate_report: dict[str, Any] | None = None,
    clock: Clock = SYSTEM_CLOCK,
) -> StageChange:
    """Move the portfolio to ``to_stage`` and log it, in one transaction.

    A promotion goes one stage up and needs ``gate_report`` (a
    ``GateReport`` as a dict) for ``to_stage`` that passed. A demotion goes
    down any number of stages and ignores ``gate_report``."""
    target = stage_index(to_stage)
    if not isinstance(reason, str) or not reason.strip():
        raise StageError("a reason is required")
    if not isinstance(actor, str) or not actor.strip():
        raise StageError("an actor is required")
    with state.transaction():
        rows = state.sql("SELECT live_stage FROM portfolios WHERE id = ?", [portfolio_id])
        if not rows:
            raise StageError(f"portfolio {portfolio_id!r} not found")
        current = cast(LiveStage, rows[0]["live_stage"])
        now = stage_index(current)
        if target == now:
            raise StageError(f"the portfolio is already in {current}")
        direction: Direction = "promote" if target > now else "demote"
        report: dict[str, Any] | None = None
        if direction == "promote":
            if target != now + 1:
                raise StageError(
                    f"promote one stage at a time: {current} leads to {next_stage(current)}"
                )
            if not gate_report:
                raise StageError(f"promoting to {to_stage} needs a gate report")
            if gate_report.get("target") != to_stage:
                raise StageError("the gate report is for another stage")
            if not gate_report.get("passed"):
                raise StageError(f"the gate to {to_stage} did not pass")
            report = gate_report
        created = iso_now(clock)
        cur = state.execute(
            f"INSERT INTO {TABLE} (portfolio_id, from_stage, to_stage, direction, actor,"
            " reason, gate_report_json, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            [
                portfolio_id,
                current,
                to_stage,
                direction,
                actor.strip(),
                reason.strip(),
                json.dumps(report, sort_keys=True, default=str) if report is not None else None,
                created,
            ],
        )
        state.execute("UPDATE portfolios SET live_stage = ? WHERE id = ?", [to_stage, portfolio_id])
        AuditLog(state).record(
            actor,
            f"live.stage_{direction}d",
            "portfolio",
            portfolio_id,
            portfolio_id=portfolio_id,
            details={
                "from_stage": current,
                "to_stage": to_stage,
                "reason": reason.strip(),
                "change_id": cur.lastrowid,
            },
        )
        row = state.sql(f"SELECT * FROM {TABLE} WHERE id = ?", [cur.lastrowid])[0]
    return _change(row)


def _change(row: Any) -> StageChange:
    raw = row["gate_report_json"]
    return StageChange(
        id=int(row["id"]),
        portfolio_id=row["portfolio_id"],
        from_stage=row["from_stage"],
        to_stage=row["to_stage"],
        direction=row["direction"],
        actor=row["actor"],
        reason=row["reason"],
        gate_report=json.loads(raw) if raw else None,
        created_at=row["created_at"],
    )

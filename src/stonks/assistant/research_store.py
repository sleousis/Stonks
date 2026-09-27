"""Research sessions and their proposals (roadmap 22.9, migration 029).

A session is one person's goal with its model, the model's training cutoff,
its budgets and what it used. A proposal is one lab trial the model asked
for, recorded with its hypothesis before it runs. Reads that name an owner
treat another person's session as missing (:class:`SessionNotFound`). Each
call opens its own state connection through ``state_factory``.
"""

from __future__ import annotations

import json
import secrets
from collections.abc import Callable
from contextlib import AbstractContextManager
from dataclasses import dataclass
from datetime import UTC, date, datetime
from typing import Any, Literal, Protocol

from stonks.store.state import SqliteState

StateFactory = Callable[[], AbstractContextManager[SqliteState]]
SessionStatus = Literal["queued", "running", "done", "stopped", "failed"]
ProposalStatus = Literal["rejected", "running", "done", "failed", "stopped"]


class SessionNotFound(LookupError):
    """No such research session, or it belongs to someone else."""


class Budgets(Protocol):
    """The budget fields of ``[assistant.research]`` (or a request)."""

    max_trials: int
    max_proposals: int
    max_cpu_seconds: float


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


@dataclass(frozen=True)
class ResearchSession:
    id: str
    owner_id: str
    goal: str
    universe: tuple[str, ...]
    universe_id: str | None
    model: str
    model_cutoff: date
    prompt_version: str
    max_trials: int
    max_proposals: int
    max_cpu_seconds: float
    trials_used: int
    cpu_seconds_used: float
    status: SessionStatus
    stop_reason: str | None
    summary: str | None
    job_id: str | None
    created_at: str
    started_at: str | None
    finished_at: str | None

    @property
    def trials_left(self) -> int:
        return max(0, self.max_trials - self.trials_used)

    @property
    def cpu_seconds_left(self) -> float:
        return max(0.0, self.max_cpu_seconds - self.cpu_seconds_used)


@dataclass(frozen=True)
class ResearchProposal:
    id: str
    session_id: str
    seq: int
    hypothesis: str | None
    premortem: str | None
    class_path: str | None
    arguments: dict[str, Any]
    status: ProposalStatus
    reason: str | None
    validation_start: str | None
    budget: int | None
    lab_run_id: str | None
    verdict: str | None
    best_score: float | None
    trials: int
    cpu_seconds: float
    outcome: dict[str, Any] | None
    created_at: str
    finished_at: str | None


def _session(row: Any) -> ResearchSession:
    return ResearchSession(
        id=row["id"],
        owner_id=row["owner_id"],
        goal=row["goal"],
        universe=tuple(json.loads(row["universe_json"] or "[]")),
        universe_id=row["universe_id"],
        model=row["model"],
        model_cutoff=date.fromisoformat(row["model_cutoff"]),
        prompt_version=row["prompt_version"],
        max_trials=int(row["max_trials"]),
        max_proposals=int(row["max_proposals"]),
        max_cpu_seconds=float(row["max_cpu_seconds"]),
        trials_used=int(row["trials_used"]),
        cpu_seconds_used=float(row["cpu_seconds_used"]),
        status=row["status"],
        stop_reason=row["stop_reason"],
        summary=row["summary"],
        job_id=row["job_id"],
        created_at=row["created_at"],
        started_at=row["started_at"],
        finished_at=row["finished_at"],
    )


def _proposal(row: Any) -> ResearchProposal:
    outcome = row["outcome_json"]
    return ResearchProposal(
        id=row["id"],
        session_id=row["session_id"],
        seq=int(row["seq"]),
        hypothesis=row["hypothesis"],
        premortem=row["premortem"],
        class_path=row["class_path"],
        arguments=json.loads(row["arguments_json"] or "{}"),
        status=row["status"],
        reason=row["reason"],
        validation_start=row["validation_start"],
        budget=row["budget"],
        lab_run_id=row["lab_run_id"],
        verdict=row["verdict"],
        best_score=row["best_score"],
        trials=int(row["trials"]),
        cpu_seconds=float(row["cpu_seconds"]),
        outcome=json.loads(outcome) if outcome else None,
        created_at=row["created_at"],
        finished_at=row["finished_at"],
    )


def _text(value: Any) -> str | None:
    return value if isinstance(value, str) else None


class ResearchStore:
    def __init__(self, state_factory: StateFactory) -> None:
        self._state = state_factory

    # ---- sessions ------------------------------------------------------------

    def create_session(
        self,
        owner_id: str,
        goal: str,
        *,
        universe: list[str] | tuple[str, ...] = (),
        universe_id: str | None = None,
        model: str,
        model_cutoff: date | None,
        budget: Budgets,
        prompt_version: str | None = None,
    ) -> ResearchSession:
        if model_cutoff is None:
            raise ValueError("a research session needs the model's training cutoff")
        if prompt_version is None:
            from stonks.assistant.research import RESEARCH_PROMPT_VERSION

            prompt_version = RESEARCH_PROMPT_VERSION
        sid = f"rs_{secrets.token_hex(8)}"
        with self._state() as state:
            state.execute(
                "INSERT INTO research_sessions (id, owner_id, goal, universe_json, universe_id,"
                " model, model_cutoff, prompt_version, max_trials, max_proposals,"
                " max_cpu_seconds, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                [
                    sid,
                    owner_id,
                    goal,
                    json.dumps(list(universe)),
                    universe_id,
                    model,
                    model_cutoff.isoformat(),
                    prompt_version,
                    int(budget.max_trials),
                    int(budget.max_proposals),
                    float(budget.max_cpu_seconds),
                    _now(),
                ],
            )
        return self.session(sid)

    def session(self, session_id: str, owner_id: str | None = None) -> ResearchSession:
        """One session; with ``owner_id`` another person's reads as missing."""
        sql = "SELECT * FROM research_sessions WHERE id = ?"
        params: list[Any] = [session_id]
        if owner_id is not None:
            sql += " AND owner_id = ?"
            params.append(owner_id)
        with self._state() as state:
            rows = state.sql(sql, params)
        if not rows:
            raise SessionNotFound(session_id)
        return _session(rows[0])

    def sessions(
        self, owner_id: str, *, limit: int = 50, offset: int = 0
    ) -> tuple[list[ResearchSession], int]:
        """A person's sessions, newest first, and their total."""
        with self._state() as state:
            rows = state.sql(
                "SELECT * FROM research_sessions WHERE owner_id = ?"
                " ORDER BY created_at DESC, id DESC LIMIT ? OFFSET ?",
                [owner_id, limit, offset],
            )
            total = state.sql(
                "SELECT COUNT(*) AS n FROM research_sessions WHERE owner_id = ?", [owner_id]
            )[0]["n"]
        return [_session(r) for r in rows], int(total)

    def mark_running(self, session_id: str, job_id: str | None = None) -> None:
        with self._state() as state:
            state.execute(
                "UPDATE research_sessions SET status = 'running', started_at = ?,"
                " job_id = COALESCE(?, job_id) WHERE id = ?",
                [_now(), job_id, session_id],
            )

    def set_job(self, session_id: str, job_id: str) -> None:
        with self._state() as state:
            state.execute(
                "UPDATE research_sessions SET job_id = ? WHERE id = ?", [job_id, session_id]
            )

    def add_usage(self, session_id: str, *, trials: int, cpu_seconds: float) -> None:
        with self._state() as state:
            state.execute(
                "UPDATE research_sessions SET trials_used = trials_used + ?,"
                " cpu_seconds_used = cpu_seconds_used + ? WHERE id = ?",
                [int(trials), float(cpu_seconds), session_id],
            )

    def finish_session(
        self,
        session_id: str,
        status: SessionStatus,
        *,
        stop_reason: str | None = None,
        summary: str | None = None,
    ) -> ResearchSession:
        with self._state() as state:
            state.execute(
                "UPDATE research_sessions SET status = ?, stop_reason = ?,"
                " summary = COALESCE(?, summary), finished_at = ? WHERE id = ?",
                [status, stop_reason, summary, _now(), session_id],
            )
        return self.session(session_id)

    # ---- proposals -----------------------------------------------------------

    def next_seq(self, session_id: str) -> int:
        with self._state() as state:
            row = state.sql(
                "SELECT COALESCE(MAX(seq), 0) AS n FROM research_proposals WHERE session_id = ?",
                [session_id],
            )[0]
        return int(row["n"]) + 1

    def add_proposal(
        self,
        session_id: str,
        arguments: dict[str, Any],
        *,
        status: ProposalStatus,
        reason: str | None = None,
        validation_start: date | None = None,
        budget: int | None = None,
    ) -> ResearchProposal:
        """Record a proposal as the model made it: before it runs
        (``running``) or with the reason it never will (``rejected``)."""
        pid = f"rp_{secrets.token_hex(8)}"
        raw_budget = arguments.get("budget")
        with self._state() as state:
            state.execute(
                "INSERT INTO research_proposals (id, session_id, seq, hypothesis, premortem,"
                " class_path, arguments_json, status, reason, validation_start, budget,"
                " created_at, finished_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                [
                    pid,
                    session_id,
                    self.next_seq(session_id),
                    _text(arguments.get("hypothesis")),
                    _text(arguments.get("premortem")),
                    _text(arguments.get("class_path")),
                    json.dumps(arguments, default=str, sort_keys=True),
                    status,
                    reason,
                    validation_start.isoformat() if validation_start else None,
                    budget
                    if budget is not None
                    else (raw_budget if isinstance(raw_budget, int) else None),
                    _now(),
                    _now() if status == "rejected" else None,
                ],
            )
        return self.proposal(pid)

    def finish_proposal(
        self,
        proposal_id: str,
        status: ProposalStatus,
        *,
        reason: str | None = None,
        trials: int = 0,
        cpu_seconds: float = 0.0,
        lab_run_id: str | None = None,
        verdict: str | None = None,
        best_score: float | None = None,
        outcome: dict[str, Any] | None = None,
    ) -> ResearchProposal:
        with self._state() as state:
            state.execute(
                "UPDATE research_proposals SET status = ?, reason = ?, trials = ?,"
                " cpu_seconds = ?, lab_run_id = ?, verdict = ?, best_score = ?,"
                " outcome_json = ?, finished_at = ? WHERE id = ?",
                [
                    status,
                    reason,
                    int(trials),
                    float(cpu_seconds),
                    lab_run_id,
                    verdict,
                    best_score,
                    json.dumps(outcome, default=str) if outcome is not None else None,
                    _now(),
                    proposal_id,
                ],
            )
        return self.proposal(proposal_id)

    def proposal(self, proposal_id: str) -> ResearchProposal:
        with self._state() as state:
            rows = state.sql("SELECT * FROM research_proposals WHERE id = ?", [proposal_id])
        if not rows:
            raise LookupError(proposal_id)
        return _proposal(rows[0])

    def proposals(self, session_id: str) -> list[ResearchProposal]:
        with self._state() as state:
            rows = state.sql(
                "SELECT * FROM research_proposals WHERE session_id = ? ORDER BY seq",
                [session_id],
            )
        return [_proposal(r) for r in rows]

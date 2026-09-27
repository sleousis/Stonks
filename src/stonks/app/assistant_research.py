"""ResearchService: the assistant's research loop (roadmap 22.9).

A person starts a research session with a goal and a universe. It runs as
the ``assistant_research`` job: the configured model proposes lab trials
and :class:`~stonks.assistant.research.ResearchLoop` checks, records and
runs each one through :class:`ServiceLabExecutor`, the same lab path as
``POST /api/lab/runs`` (ledger, preflight, costs), never registering.

Budgets a request gives can only lower the ``[assistant.research]`` ones.
The loop needs the model's training cutoff: without it the service answers
``not_configured``. A frozen assistant starts no session, and a freeze
during one stops it before its next run.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, date, datetime
from typing import Any, Literal, Self, cast

import anyio
from pydantic import BaseModel, ConfigDict, Field, model_validator

from stonks.app.catalog import CatalogService
from stonks.app.context import AppContext
from stonks.app.errors import ConfigurationError, ConflictError, NotFoundError
from stonks.app.jobs import Job, JobContext, JobRunner
from stonks.app.lab import LabRunRequest, LabService
from stonks.app.pagination import Page
from stonks.app.strategies import StrategyService
from stonks.assistant import guard
from stonks.assistant.research import (
    LabExecutor,
    LabOutcome,
    ResearchLoop,
    StrategyChoice,
    TrialProposal,
)
from stonks.assistant.research_store import (
    ResearchProposal,
    ResearchSession,
    ResearchStore,
    SessionNotFound,
)
from stonks.assistant.settings import AssistantConfig
from stonks.auth.policy import Permission, require
from stonks.auth.principal import Principal
from stonks.lab.trials import TrialLedger
from stonks.logging import get_logger
from stonks.universes.base import UNIVERSE_ID_PATTERN

_log = get_logger("stonks.app.assistant_research")

RESEARCH_JOB = "assistant_research"

NO_ENDPOINT = (
    "the assistant is off: set [assistant] base_url to an OpenAI-compatible endpoint "
    "(Ollama, vLLM or a llama.cpp server)"
)
NO_CUTOFF = (
    "the research loop is off: set [assistant.research] model_cutoff to your model's "
    "training cutoff (only data after it counts as out-of-sample evidence)"
)


# ---- requests and views -----------------------------------------------------------


class ResearchStart(BaseModel):
    """A research session: what to look for, on which universe, and budgets
    that may only be lower than the configured ones."""

    model_config = ConfigDict(extra="forbid")

    goal: str = Field(min_length=10, max_length=2_000, description="What to look for.")
    universe: list[str] = Field(default_factory=list[str], max_length=500)
    universe_id: str | None = Field(default=None, pattern=UNIVERSE_ID_PATTERN)
    max_trials: int | None = Field(default=None, ge=1, description="At most the setting.")
    max_proposals: int | None = Field(default=None, ge=1, description="At most the setting.")
    max_cpu_seconds: float | None = Field(default=None, gt=0, description="At most the setting.")

    @model_validator(mode="after")
    def _has_a_universe(self) -> Self:
        if not self.universe and not self.universe_id:
            raise ValueError("give universe (tickers) or universe_id")
        return self


class ResearchSessionView(BaseModel):
    id: str
    goal: str
    universe: list[str]
    universe_id: str | None
    model: str
    model_cutoff: date = Field(
        description="The model's training cutoff: validation windows start after it."
    )
    prompt_version: str
    max_trials: int
    max_proposals: int
    max_cpu_seconds: float
    trials_used: int
    cpu_seconds_used: float
    status: Literal["queued", "running", "done", "stopped", "failed"]
    stop_reason: str | None
    summary: str | None
    job_id: str | None
    created_at: datetime
    started_at: datetime | None
    finished_at: datetime | None


class ResearchProposalView(BaseModel):
    id: str
    seq: int
    hypothesis: str | None
    premortem: str | None
    class_path: str | None
    arguments: dict[str, Any] = Field(description="What the model proposed.")
    status: Literal["rejected", "running", "done", "failed", "stopped"]
    reason: str | None = Field(description="Why it was rejected, stopped or failed.")
    validation_start: date | None
    budget: int | None
    lab_run_id: str | None = Field(description="The run in the trial ledger.")
    verdict: str | None
    best_score: float | None
    trials: int
    cpu_seconds: float
    outcome: dict[str, Any] | None
    created_at: datetime
    finished_at: datetime | None


class ResearchSessionDetailView(ResearchSessionView):
    proposals: list[ResearchProposalView]


def _dt(value: str | None) -> datetime | None:
    return datetime.fromisoformat(value) if value else None


def session_view(s: ResearchSession) -> ResearchSessionView:
    return ResearchSessionView(
        id=s.id,
        goal=s.goal,
        universe=list(s.universe),
        universe_id=s.universe_id,
        model=s.model,
        model_cutoff=s.model_cutoff,
        prompt_version=s.prompt_version,
        max_trials=s.max_trials,
        max_proposals=s.max_proposals,
        max_cpu_seconds=s.max_cpu_seconds,
        trials_used=s.trials_used,
        cpu_seconds_used=s.cpu_seconds_used,
        status=s.status,
        stop_reason=s.stop_reason,
        summary=s.summary,
        job_id=s.job_id,
        created_at=datetime.fromisoformat(s.created_at),
        started_at=_dt(s.started_at),
        finished_at=_dt(s.finished_at),
    )


def proposal_view(p: ResearchProposal) -> ResearchProposalView:
    return ResearchProposalView(
        id=p.id,
        seq=p.seq,
        hypothesis=p.hypothesis,
        premortem=p.premortem,
        class_path=p.class_path,
        arguments=p.arguments,
        status=p.status,
        reason=p.reason,
        validation_start=date.fromisoformat(p.validation_start) if p.validation_start else None,
        budget=p.budget,
        lab_run_id=p.lab_run_id,
        verdict=p.verdict,
        best_score=p.best_score,
        trials=p.trials,
        cpu_seconds=p.cpu_seconds,
        outcome=p.outcome,
        created_at=datetime.fromisoformat(p.created_at),
        finished_at=_dt(p.finished_at),
    )


# ---- the lab seam -----------------------------------------------------------------


class _Checkpoints:
    """Stands in for the lab's job context: the lab calls
    ``check_cancelled`` between trials and tests, which runs the research
    checkpoint (budget, cancel) and the job's own cancel check."""

    def __init__(self, job: JobContext | None, checkpoint: Callable[[], None]) -> None:
        self._job = job
        self._checkpoint = checkpoint

    def progress(self, fraction: float, message: str | None = None) -> None:
        if self._job is not None and message:
            self._job.progress(0.5, f"research trial: {message}")

    def check_cancelled(self) -> None:
        self._checkpoint()
        if self._job is not None:
            self._job.check_cancelled()


class ServiceLabExecutor(LabExecutor):
    """Runs a proposal through :meth:`LabService.run_lab_class`: the normal
    lab path with the ledger, in the session's trial family, never
    registering."""

    def __init__(
        self,
        context: AppContext,
        lab: LabService,
        strategies: StrategyService,
        catalog: CatalogService,
        job: JobContext | None = None,
    ) -> None:
        self._ctx = context
        self._lab = lab
        self._strategies = strategies
        self._catalog = catalog
        self._job = job

    def strategies(self) -> list[StrategyChoice]:
        return [
            StrategyChoice(info.class_path, _first_line(info.description))
            for info in self._catalog.strategies()
        ]

    def request(self, proposal: TrialProposal, session: ResearchSession) -> LabRunRequest:
        """The lab request for a proposal. Research settings pick the tuner
        and the suite. Nothing registers."""
        research = self._ctx.settings.assistant.research
        return LabRunRequest.model_validate(
            {
                "strategy": {"class_path": proposal.class_path},
                "universe": list(session.universe),
                "universe_id": session.universe_id,
                "start": proposal.start.isoformat(),
                "end": proposal.end.isoformat(),
                "train_ratio": proposal.train_ratio,
                "tuner": "random",
                "budget": proposal.budget,
                "survival_tests": list(research.survival_tests),
                "hypothesis": proposal.hypothesis,
                "premortem": proposal.premortem,
                "register_strategy": False,
                "register_if_passes": False,
            }
        )

    def run(
        self,
        proposal: TrialProposal,
        *,
        session: ResearchSession,
        checkpoint: Callable[[], None],
    ) -> LabOutcome:
        request = self.request(proposal, session)
        cls = self._strategies.strategy_class(request.strategy)
        checkpoint()
        view = self._lab.run_lab_class(
            cls,
            request,
            progress=cast(JobContext, _Checkpoints(self._job, checkpoint)),
            family=session.id,
        )
        if view.registered_strategy_id is not None:  # pragma: no cover - defence in depth
            raise RuntimeError("a research run registered a strategy")
        return LabOutcome(
            run_id=view.run_id,
            verdict=view.verdict,
            best_score=view.best_score,
            best_params=dict(view.best_params),
            n_trials_run=view.n_trials_run,
            n_trials_class=view.n_trials_class,
            n_trials_family=view.n_trials_family,
            reports=[
                {"test_id": r.test_id, "passed": r.passed, "notes": r.notes}
                for r in view.survival_reports
            ],
        )

    def count_unscored(self, family: str) -> int:
        with self._ctx.state() as state:
            ledger = TrialLedger(state, self._ctx.settings.registry.artifacts_dir)
            return ledger.count_unscored(family)


def _first_line(text: str) -> str:
    line = (text or "").strip().splitlines()
    return line[0][:160] if line else ""


# ---- the service ------------------------------------------------------------------

ModelFactory = Callable[[AssistantConfig], Any]


class ResearchService:
    def __init__(
        self,
        context: AppContext,
        lab: LabService,
        strategies: StrategyService,
        catalog: CatalogService,
        runner: JobRunner,
        *,
        model_factory: Callable[[], ModelFactory],
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        """``model_factory`` returns the assistant's current model factory,
        so a test that swaps the assistant's model swaps this one too."""
        self._ctx = context
        self._lab = lab
        self._strategies = strategies
        self._catalog = catalog
        self._runner = runner
        self._model_factory = model_factory
        self._clock = clock or (lambda: datetime.now(UTC))
        self._store = ResearchStore(context.state)
        runner.register(RESEARCH_JOB, self._handle, cancellable=True)

    @property
    def config(self) -> AssistantConfig:
        return self._ctx.settings.assistant

    # ---- requests ----------------------------------------------------------------

    def start(self, principal: Principal, body: ResearchStart) -> Job:
        """Queue a research session as a job (research only, never registers)."""
        require(principal, Permission.LAB_RUN)
        cfg = self.config
        if not cfg.enabled:
            raise ConfigurationError(NO_ENDPOINT)
        research = cfg.research
        if research.model_cutoff is None:
            raise ConfigurationError(NO_CUTOFF)
        with self._ctx.state() as state:
            gate = guard.gate_for(state, principal.user_id, cfg.envelope, now=self._clock())
        if gate.frozen_until is not None:
            raise ConflictError(
                f"your assistant is frozen until {gate.frozen_until}: {gate.reason}"
            )
        budget = _Budget(
            max_trials=_lower(body.max_trials, research.max_trials),
            max_proposals=_lower(body.max_proposals, research.max_proposals),
            max_cpu_seconds=_lower(body.max_cpu_seconds, research.max_cpu_seconds),
        )
        session = self._store.create_session(
            principal.user_id,
            body.goal,
            universe=[t.strip().upper() for t in body.universe if t.strip()],
            universe_id=body.universe_id,
            model=cfg.model,
            model_cutoff=research.model_cutoff,
            budget=budget,
        )
        job = self._runner.submit(
            RESEARCH_JOB, {"session_id": session.id}, owner_id=principal.user_id
        )
        self._store.set_job(session.id, job.id)
        _log.info("research.queued", session_id=session.id, job_id=job.id)
        return job

    def list(self, principal: Principal, *, limit: int, offset: int) -> Page[ResearchSessionView]:
        require(principal, Permission.READ)
        rows, total = self._store.sessions(principal.user_id, limit=limit, offset=offset)
        return Page[ResearchSessionView](
            items=[session_view(s) for s in rows], total=total, limit=limit, offset=offset
        )

    def get(self, principal: Principal, session_id: str) -> ResearchSessionDetailView:
        require(principal, Permission.READ)
        try:
            session = self._store.session(session_id, principal.user_id)
        except SessionNotFound:
            raise NotFoundError(f"no research session {session_id!r}") from None
        return ResearchSessionDetailView(
            **session_view(session).model_dump(),
            proposals=[proposal_view(p) for p in self._store.proposals(session.id)],
        )

    # ---- the job ------------------------------------------------------------------

    def _handle(self, params: dict[str, Any], ctx: JobContext) -> ResearchSessionView:
        session_id = str(params["session_id"])
        session = self._store.session(session_id)
        owner = session.owner_id
        cfg = self.config

        def frozen() -> str | None:
            try:
                with self._ctx.state() as state:
                    found = guard.frozen_until(state, owner, self._clock())
            except Exception as exc:  # fail closed
                _log.error("research.freeze_check_failed", error=str(exc))
                return "unchecked: the safety check failed"
            return f"frozen until {found[0]} ({found[1]})" if found else None

        loop = ResearchLoop(
            self._model_factory()(cfg),
            ServiceLabExecutor(self._ctx, self._lab, self._strategies, self._catalog, ctx),
            self._store,
            cfg,
            workers=self._ctx.settings.lab.parallel.resolved_workers(),
            frozen=frozen,
            cancelled=lambda: ctx.cancel_requested,
        )
        try:
            final = anyio.run(loop.run, session_id)
        except BaseException as exc:
            self._store.finish_session(session_id, "failed", stop_reason=str(exc) or "crashed")
            raise
        ctx.check_cancelled()
        return session_view(final)


class _Budget(BaseModel):
    max_trials: int
    max_proposals: int
    max_cpu_seconds: float


def _lower[T: (int, float)](asked: T | None, limit: T) -> T:
    return limit if asked is None else min(asked, limit)

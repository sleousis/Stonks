"""Strategy Studio: rule templates + schema, drafts, validation, backtest
and lab-run jobs, registration and enable / disable.

Code-draft operations answer 403 while ``api.allow_code_strategies`` is
off. Every non-GET route needs the bearer token (method-based auth).
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from fastapi import APIRouter, HTTPException, Response

from stonks.api.deps import PageDep, ServicesDep
from stonks.api.errors import PROBLEM_RESPONSES, ProblemDetails
from stonks.api.routers._jobs_common import JOB_CREATED, accepted
from stonks.app.jobs import Job
from stonks.app.pagination import Page
from stonks.app.studio import (
    CodeStrategiesDisabledError,
    Draft,
    DraftBacktestRequest,
    DraftCreate,
    DraftLabRunRequest,
    DraftUpdate,
    DraftValidation,
    RuleTemplateView,
    SpecValidateRequest,
    StudioService,
    ValidateRequest,
    studio_service,
)

router = APIRouter(
    prefix="/api/studio",
    tags=["studio"],
    responses={
        **PROBLEM_RESPONSES,
        403: {"model": ProblemDetails, "description": "Code strategies are disabled"},
    },
)


def _call[T](services: Any, fn: Callable[[StudioService], T]) -> T:
    try:
        return fn(studio_service(services))
    except CodeStrategiesDisabledError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from None


@router.get("/templates", response_model=list[RuleTemplateView], operation_id="listStudioTemplates")
def templates(services: ServicesDep) -> list[RuleTemplateView]:
    """Starter rule specs for the rule builder."""
    return _call(services, lambda s: s.templates())


@router.get("/schema", response_model=dict[str, Any], operation_id="getRuleSpecSchema")
def schema(services: ServicesDep) -> dict[str, Any]:
    """JSON Schema of a rule spec (version 1)."""
    return _call(services, lambda s: s.schema())


@router.post("/spec/validate", response_model=DraftValidation, operation_id="validateRuleSpec")
def validate_spec(body: SpecValidateRequest, services: ServicesDep) -> DraftValidation:
    """Validate a rule spec without saving it (no smoke run)."""
    return _call(services, lambda s: s.validate_spec(body.spec))


@router.get("/drafts", response_model=Page[Draft], operation_id="listDrafts")
def list_drafts(services: ServicesDep, page: PageDep) -> Page[Draft]:
    return _call(services, lambda s: s.list_drafts(limit=page.limit, offset=page.offset))


@router.post("/drafts", response_model=Draft, status_code=201, operation_id="createDraft")
def create_draft(body: DraftCreate, services: ServicesDep) -> Draft:
    return _call(services, lambda s: s.create_draft(body))


@router.get("/drafts/{draft_id}", response_model=Draft, operation_id="getDraft")
def get_draft(draft_id: str, services: ServicesDep) -> Draft:
    return _call(services, lambda s: s.get_draft(draft_id))


@router.patch("/drafts/{draft_id}", response_model=Draft, operation_id="updateDraft")
def update_draft(draft_id: str, body: DraftUpdate, services: ServicesDep) -> Draft:
    return _call(services, lambda s: s.update_draft(draft_id, body))


@router.delete("/drafts/{draft_id}", response_model=Draft, operation_id="deleteDraft")
def delete_draft(draft_id: str, services: ServicesDep) -> Draft:
    """Delete a draft (returns it); a strategy registered from it stays."""
    return _call(services, lambda s: s.delete_draft(draft_id))


@router.post(
    "/drafts/{draft_id}/validate", response_model=DraftValidation, operation_id="validateDraft"
)
def validate_draft(
    draft_id: str, services: ServicesDep, body: ValidateRequest | None = None
) -> DraftValidation:
    """Validate the draft and smoke-run ``estimate_return`` (on the given
    lake tickers, or on synthetic sample data)."""
    return _call(services, lambda s: s.validate_draft(draft_id, body or ValidateRequest()))


@router.post("/drafts/{draft_id}/backtests", **JOB_CREATED, operation_id="startDraftBacktest")
def start_backtest(
    draft_id: str, body: DraftBacktestRequest, services: ServicesDep, response: Response
) -> Job:
    """Queue a backtest of the draft; the job result is a ``BacktestResult``."""
    return accepted(_call(services, lambda s: s.submit_backtest(draft_id, body)), response)


@router.post("/drafts/{draft_id}/lab-runs", **JOB_CREATED, operation_id="startDraftLabRun")
def start_lab_run(
    draft_id: str, body: DraftLabRunRequest, services: ServicesDep, response: Response
) -> Job:
    """Queue tune → fit → survival suite for the draft; the job result is a
    ``LabRunView``."""
    return accepted(_call(services, lambda s: s.submit_lab_run(draft_id, body)), response)


@router.post("/drafts/{draft_id}/register", response_model=Draft, operation_id="registerDraft")
def register_draft(draft_id: str, services: ServicesDep) -> Draft:
    """Register the draft's strategy in ``shadow``."""
    return _call(services, lambda s: s.register_draft(draft_id))


@router.post("/drafts/{draft_id}/enable", response_model=Draft, operation_id="enableDraft")
def enable_draft(draft_id: str, services: ServicesDep) -> Draft:
    """Promote the registered strategy to ``active``."""
    return _call(services, lambda s: s.enable(draft_id))


@router.post("/drafts/{draft_id}/disable", response_model=Draft, operation_id="disableDraft")
def disable_draft(draft_id: str, services: ServicesDep) -> Draft:
    """Move the registered strategy back to ``shadow``."""
    return _call(services, lambda s: s.disable(draft_id))

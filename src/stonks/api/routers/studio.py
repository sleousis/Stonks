"""Strategy Studio: rule templates + schema, drafts, validation, backtest
and lab-run jobs, registration and enable / disable.

Code-draft operations answer 403 while ``api.allow_code_strategies`` is
off, and are for admins only (code runs with the server's rights). Rule
drafts need ``lab.run``. Registering, enabling and disabling change the
shared catalog, so they need ``strategy.promote`` (admins).
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Annotated, Any

from fastapi import APIRouter, Body, HTTPException, Response

from stonks.api.deps import PageDep, PrincipalDep, ServicesDep, needs
from stonks.api.errors import PROBLEM_RESPONSES, ProblemDetails
from stonks.api.routers._jobs_common import JOB_CREATED, accepted
from stonks.app.jobs import Job
from stonks.app.pagination import Page
from stonks.app.strategies import StatusChangeRequest
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
    StudioCapabilities,
    StudioService,
    ValidateRequest,
    studio_service,
)
from stonks.auth import Permission, Principal, require

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


def _guard_code(services: Any, principal: Principal, draft_id: str) -> None:
    """Code drafts are admin-only (design section 10)."""
    draft = _call(services, lambda s: s.get_draft(draft_id))
    if draft.kind == "code":
        require(principal, Permission.CODE_STRATEGIES)


@router.get(
    "/capabilities", response_model=StudioCapabilities, operation_id="getStudioCapabilities"
)
def capabilities(services: ServicesDep) -> StudioCapabilities:
    """Which Studio features this server allows (e.g. code strategies), so a
    client can hide what would answer 403."""
    return _call(services, lambda s: s.capabilities())


@router.get("/templates", response_model=list[RuleTemplateView], operation_id="listStudioTemplates")
def templates(services: ServicesDep) -> list[RuleTemplateView]:
    """Starter rule specs for the rule builder."""
    return _call(services, lambda s: s.templates())


@router.get("/schema", response_model=dict[str, Any], operation_id="getRuleSpecSchema")
def schema(services: ServicesDep) -> dict[str, Any]:
    """JSON Schema of a rule spec (version 1)."""
    return _call(services, lambda s: s.schema())


@router.post(
    "/spec/validate",
    response_model=DraftValidation,
    operation_id="validateRuleSpec",
    dependencies=needs(Permission.LAB_RUN),
)
def validate_spec(body: SpecValidateRequest, services: ServicesDep) -> DraftValidation:
    """Validate a rule spec without saving it (no smoke run)."""
    return _call(services, lambda s: s.validate_spec(body.spec))


@router.get("/drafts", response_model=Page[Draft], operation_id="listDrafts")
def list_drafts(services: ServicesDep, page: PageDep) -> Page[Draft]:
    return _call(services, lambda s: s.list_drafts(limit=page.limit, offset=page.offset))


@router.post(
    "/drafts",
    response_model=Draft,
    status_code=201,
    operation_id="createDraft",
    dependencies=needs(Permission.LAB_RUN),
)
def create_draft(body: DraftCreate, services: ServicesDep, principal: PrincipalDep) -> Draft:
    if body.kind == "code":
        require(principal, Permission.CODE_STRATEGIES)
    return _call(services, lambda s: s.create_draft(body))


@router.get("/drafts/{draft_id}", response_model=Draft, operation_id="getDraft")
def get_draft(draft_id: str, services: ServicesDep) -> Draft:
    return _call(services, lambda s: s.get_draft(draft_id))


@router.patch(
    "/drafts/{draft_id}",
    response_model=Draft,
    operation_id="updateDraft",
    dependencies=needs(Permission.LAB_RUN),
)
def update_draft(
    draft_id: str, body: DraftUpdate, services: ServicesDep, principal: PrincipalDep
) -> Draft:
    _guard_code(services, principal, draft_id)
    return _call(services, lambda s: s.update_draft(draft_id, body))


@router.delete(
    "/drafts/{draft_id}",
    response_model=Draft,
    operation_id="deleteDraft",
    dependencies=needs(Permission.LAB_RUN),
)
def delete_draft(draft_id: str, services: ServicesDep, principal: PrincipalDep) -> Draft:
    """Delete a draft (returns it); a strategy registered from it stays."""
    _guard_code(services, principal, draft_id)
    return _call(services, lambda s: s.delete_draft(draft_id))


@router.post(
    "/drafts/{draft_id}/validate",
    response_model=DraftValidation,
    operation_id="validateDraft",
    dependencies=needs(Permission.LAB_RUN),
)
def validate_draft(
    draft_id: str,
    services: ServicesDep,
    principal: PrincipalDep,
    body: ValidateRequest | None = None,
) -> DraftValidation:
    """Validate the draft and smoke-run ``estimate_return`` (on the given
    lake tickers, or on synthetic sample data)."""
    _guard_code(services, principal, draft_id)
    return _call(services, lambda s: s.validate_draft(draft_id, body or ValidateRequest()))


@router.post(
    "/drafts/{draft_id}/backtests",
    **JOB_CREATED,
    operation_id="startDraftBacktest",
    dependencies=needs(Permission.LAB_RUN),
)
def start_backtest(
    draft_id: str,
    body: DraftBacktestRequest,
    services: ServicesDep,
    principal: PrincipalDep,
    response: Response,
) -> Job:
    """Queue a backtest of the draft; the job result is a ``BacktestResult``."""
    _guard_code(services, principal, draft_id)
    return accepted(_call(services, lambda s: s.submit_backtest(draft_id, body)), response)


@router.post(
    "/drafts/{draft_id}/lab-runs",
    **JOB_CREATED,
    operation_id="startDraftLabRun",
    dependencies=needs(Permission.LAB_RUN),
)
def start_lab_run(
    draft_id: str,
    body: DraftLabRunRequest,
    services: ServicesDep,
    principal: PrincipalDep,
    response: Response,
) -> Job:
    """Queue tune → fit → survival suite for the draft; the job result is a
    ``LabRunView``. Registering the result needs an admin."""
    _guard_code(services, principal, draft_id)
    if body.registers:
        require(principal, Permission.STRATEGY_PROMOTE)
    return accepted(_call(services, lambda s: s.submit_lab_run(draft_id, body)), response)


@router.post(
    "/drafts/{draft_id}/register",
    response_model=Draft,
    operation_id="registerDraft",
    dependencies=needs(Permission.STRATEGY_PROMOTE),
)
def register_draft(draft_id: str, services: ServicesDep) -> Draft:
    """Register the draft's strategy in ``shadow``."""
    return _call(services, lambda s: s.register_draft(draft_id))


@router.post(
    "/drafts/{draft_id}/enable",
    response_model=Draft,
    operation_id="enableDraft",
    dependencies=needs(Permission.STRATEGY_PROMOTE),
)
def enable_draft(
    draft_id: str,
    services: ServicesDep,
    principal: PrincipalDep,
    body: Annotated[StatusChangeRequest | None, Body()] = None,
) -> Draft:
    """Promote the registered strategy to ``active``: the same go-live gate
    as ``POST /api/strategies/{id}/promote`` (409 when refused; ``override``
    needs a ``reason`` of at least 20 characters)."""
    body = body or StatusChangeRequest()
    return _call(
        services,
        lambda s: s.enable(
            draft_id, reason=body.reason, override=body.override, actor=principal.actor
        ),
    )


@router.post(
    "/drafts/{draft_id}/disable",
    response_model=Draft,
    operation_id="disableDraft",
    dependencies=needs(Permission.STRATEGY_PROMOTE),
)
def disable_draft(
    draft_id: str,
    services: ServicesDep,
    principal: PrincipalDep,
    body: Annotated[StatusChangeRequest | None, Body()] = None,
) -> Draft:
    """Move the registered strategy back to ``shadow``; needs a ``reason``
    (422 without one)."""
    body = body or StatusChangeRequest()
    return _call(services, lambda s: s.disable(draft_id, reason=body.reason, actor=principal.actor))

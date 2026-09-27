"""The first-run guide (roadmap 13.2): your steps, and for admins the
system checklist. Everything here is about the caller."""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Path

from stonks.api.deps import PrincipalDep, ServicesDep, needs
from stonks.api.errors import PROBLEM_RESPONSES
from stonks.app.onboarding import (
    OnboardingService,
    OnboardingUpdate,
    OnboardingView,
    StepId,
    StepUpdate,
    SystemChecklistView,
)
from stonks.auth import Permission

router = APIRouter(prefix="/api/onboarding", tags=["onboarding"], responses=PROBLEM_RESPONSES)


def _service(services: ServicesDep) -> OnboardingService:
    return OnboardingService(services.context, sources=services.ingest.sources)


@router.get("", response_model=OnboardingView, operation_id="getOnboarding")
def get_onboarding(services: ServicesDep, principal: PrincipalDep) -> OnboardingView:
    """Your first-run steps (account, portfolio, data, follow, alerts), each
    done, skipped or still to do, and whether to show the guide."""
    return _service(services).get(principal)


@router.put(
    "",
    response_model=OnboardingView,
    operation_id="updateOnboarding",
    dependencies=needs(Permission.READ),
)
def update_onboarding(
    body: OnboardingUpdate, services: ServicesDep, principal: PrincipalDep
) -> OnboardingView:
    """Close the guide (``dismissed: true``) or bring it back."""
    return _service(services).update(principal, body)


@router.put(
    "/steps/{step}",
    response_model=OnboardingView,
    operation_id="updateOnboardingStep",
    dependencies=needs(Permission.READ),
)
def update_onboarding_step(
    step: Annotated[StepId, Path(description="the step to mark")],
    body: StepUpdate,
    services: ServicesDep,
    principal: PrincipalDep,
) -> OnboardingView:
    """Mark one of your steps done or skipped, or back to ``todo``. A step
    the data shows done stays done."""
    return _service(services).set_step(principal, step, body)


@router.get(
    "/system",
    response_model=SystemChecklistView,
    operation_id="getSystemChecklist",
    dependencies=needs(Permission.OPERATIONS_RUN),
)
def get_system_checklist(services: ServicesDep, principal: PrincipalDep) -> SystemChecklistView:
    """Admins: is the install ready? A data source key, a first data load,
    a backup on disk and a running scheduler."""
    return _service(services).system(principal)

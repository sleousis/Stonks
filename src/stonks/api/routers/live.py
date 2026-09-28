"""A live portfolio's owner settings (roadmap 19.6, 19.7, 19.9): the
allocation Stonks may trade, the account profile that picks the account
rules, the live stage with its gate reports, and the dry-run preview.
Changes and promotions need a fresh second factor (``live.manage``) and
are audited. A demotion and a preview need ``portfolio.trade``."""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Path, Query

from stonks.api.deps import PrincipalDep, ServicesDep, needs
from stonks.api.errors import PROBLEM_RESPONSES
from stonks.app.live import (
    AccountProfileBody,
    AccountProfileView,
    GateReportView,
    LiveAllocationUpdate,
    LiveAllocationView,
    LivePreviewView,
    LiveRulesView,
    LiveService,
    LiveStageView,
    StageDemoteBody,
    StagePromoteBody,
)
from stonks.app.options_live import OptionsApprovalUpdate, OptionsLiveService, OptionsLiveView
from stonks.auth import Permission

router = APIRouter(prefix="/api/portfolios", tags=["live"], responses=PROBLEM_RESPONSES)

PortfolioId = Annotated[str, Path(max_length=64)]


def _service(services: ServicesDep) -> LiveService:
    return LiveService(services.context)


@router.get(
    "/{portfolio_id}/live/allocation",
    response_model=LiveAllocationView,
    operation_id="getLiveAllocation",
)
def get_live_allocation(
    portfolio_id: PortfolioId, services: ServicesDep, principal: PrincipalDep
) -> LiveAllocationView:
    """How much Stonks may trade in this live portfolio. ``amount`` is
    ``null`` until you set it, and then nothing opens."""
    return _service(services).get_allocation(principal, portfolio_id)


@router.put(
    "/{portfolio_id}/live/allocation",
    response_model=LiveAllocationView,
    operation_id="setLiveAllocation",
    dependencies=needs(Permission.LIVE_MANAGE),
)
def set_live_allocation(
    portfolio_id: PortfolioId,
    body: LiveAllocationUpdate,
    services: ServicesDep,
    principal: PrincipalDep,
) -> LiveAllocationView:
    """Set the amount by hand, with a reason. Needs a fresh second factor.
    The book's gross exposure is capped at it (``capital_ramp``)."""
    return _service(services).set_allocation(principal, portfolio_id, body)


@router.get(
    "/{portfolio_id}/live/account-profile",
    response_model=AccountProfileView,
    operation_id="getAccountProfile",
)
def get_account_profile(
    portfolio_id: PortfolioId, services: ServicesDep, principal: PrincipalDep
) -> AccountProfileView:
    """The account profile (404 until one is set)."""
    return _service(services).get_profile(principal, portfolio_id)


@router.put(
    "/{portfolio_id}/live/account-profile",
    response_model=AccountProfileView,
    operation_id="setAccountProfile",
    dependencies=needs(Permission.LIVE_MANAGE),
)
def set_account_profile(
    portfolio_id: PortfolioId,
    body: AccountProfileBody,
    services: ServicesDep,
    principal: PrincipalDep,
) -> AccountProfileView:
    """Set where the account is held and its type. Shorts need a margin
    account. Needs a fresh second factor."""
    return _service(services).set_profile(principal, portfolio_id, body)


@router.get(
    "/{portfolio_id}/live/rules",
    response_model=LiveRulesView,
    operation_id="getLiveRules",
    dependencies=needs(Permission.READ),
)
def get_live_rules(
    portfolio_id: PortfolioId, services: ServicesDep, principal: PrincipalDep
) -> LiveRulesView:
    """Which live safeguards and account rules act on this portfolio, as its
    book follows them. Read only: the limits are set by the admin and your
    own risk limits."""
    return _service(services).rules(principal, portfolio_id)


# ---- stages, gates and the preview (19.9) --------------------------------------------


@router.get(
    "/{portfolio_id}/live/stage",
    response_model=LiveStageView,
    operation_id="getLiveStage",
    dependencies=needs(Permission.READ),
)
def get_live_stage(
    portfolio_id: PortfolioId,
    services: ServicesDep,
    principal: PrincipalDep,
    days: Annotated[int, Query(ge=1, le=260)] = 30,
) -> LiveStageView:
    """The portfolio's live stage, its changes and the last sessions' gate
    metrics (orders, rejections, stuck orders, fill quality, tracking and
    drift)."""
    return _service(services).stage(principal, portfolio_id, days)


@router.get(
    "/{portfolio_id}/live/gate-report",
    response_model=GateReportView,
    operation_id="getLiveGateReport",
    dependencies=needs(Permission.READ),
)
def get_live_gate_report(
    portfolio_id: PortfolioId, services: ServicesDep, principal: PrincipalDep
) -> GateReportView:
    """What a promotion to the next stage needs, checked now. A check with
    ``passed: null`` has no data yet and does not block."""
    return _service(services).gate_report(principal, portfolio_id)


@router.post(
    "/{portfolio_id}/live/stage/promote",
    response_model=LiveStageView,
    operation_id="promoteLiveStage",
    dependencies=needs(Permission.LIVE_MANAGE),
)
def promote_live_stage(
    portfolio_id: PortfolioId,
    body: StagePromoteBody,
    services: ServicesDep,
    principal: PrincipalDep,
) -> LiveStageView:
    """One stage up. The gate report is computed now and must pass. Type
    the target stage in ``confirm``. Needs a fresh second factor."""
    return _service(services).promote(principal, portfolio_id, body)


@router.post(
    "/{portfolio_id}/live/stage/demote",
    response_model=LiveStageView,
    operation_id="demoteLiveStage",
    dependencies=needs(Permission.PORTFOLIO_TRADE),
)
def demote_live_stage(
    portfolio_id: PortfolioId,
    body: StageDemoteBody,
    services: ServicesDep,
    principal: PrincipalDep,
) -> LiveStageView:
    """Down to any lower stage, with a reason. It only reduces risk, so it
    needs no second factor."""
    return _service(services).demote(principal, portfolio_id, body)


@router.post(
    "/{portfolio_id}/live/preview",
    response_model=LivePreviewView,
    operation_id="previewLiveOrders",
    dependencies=needs(Permission.PORTFOLIO_TRADE),
)
def preview_live_orders(
    portfolio_id: PortfolioId, services: ServicesDep, principal: PrincipalDep
) -> LivePreviewView:
    """The orders the live book would send now: a dry run through every
    rule and the broker's what-if. It never transmits an order."""
    return _service(services).preview(principal, portfolio_id)


# ---- live options (17.8) ----------------------------------------------------------------


@router.get(
    "/{portfolio_id}/live/options",
    response_model=OptionsLiveView,
    operation_id="getOptionsLive",
    dependencies=needs(Permission.READ),
)
def get_options_live(
    portfolio_id: PortfolioId, services: ServicesDep, principal: PrincipalDep
) -> OptionsLiveView:
    """Whether an option order may open in this portfolio, and every
    reason it may not: the admin's switch, the live stage and the options
    approval level. Off by default."""
    return OptionsLiveService(services.context).view(principal, portfolio_id)


@router.put(
    "/{portfolio_id}/live/options/approval",
    response_model=OptionsLiveView,
    operation_id="setOptionsApproval",
    dependencies=needs(Permission.LIVE_MANAGE),
)
def set_options_approval(
    portfolio_id: PortfolioId,
    body: OptionsApprovalUpdate,
    services: ServicesDep,
    principal: PrincipalDep,
) -> OptionsLiveView:
    """Set the options approval level (none, covered, spreads or naked)
    with a reason. Needs a fresh second factor. It never turns options on
    by itself: the switch and the stage still apply."""
    return OptionsLiveService(services.context).set_level(principal, portfolio_id, body)

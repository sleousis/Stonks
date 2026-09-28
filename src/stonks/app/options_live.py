"""OptionsLiveService: where a portfolio stands for live options (roadmap 17.8).

Live options are off by default. The view says whether an option order
may open in the portfolio and, when not, every reason: the
``[production.options] live`` switch, the live stage (``live_small`` or
higher) and the options approval level.

Reading needs ``data.read`` and the portfolio. Setting the approval level
needs ``live.manage``: the owner with a fresh second factor in the web app
(403 ``step_up_required`` otherwise, and always for API tokens), with a
reason. Every change writes an ``audit_log`` row.
"""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field

from stonks.accounts import NotFound, owned_portfolio
from stonks.app.context import AppContext
from stonks.app.errors import NotFoundError, ValidationError
from stonks.auth.policy import Permission, require
from stonks.auth.principal import Principal
from stonks.options.live.approval import (
    LEVELS,
    ApprovalError,
    ApprovalLevel,
    get_approval,
    set_approval,
)
from stonks.options.live.gate import options_live_state
from stonks.production.live.stages import get_stage, stages_enabled
from stonks.store.state import SqliteState

#: What each approval level lets a portfolio open.
LEVEL_TEXT: dict[str, str] = {
    "none": "No option order opens. Closes still go out.",
    "covered": "Covered calls, cash-secured puts, long calls and puts, protective puts.",
    "spreads": "Everything in covered, plus verticals and iron condors.",
    "naked": "Everything in spreads, plus uncovered short puts. Naked calls stay refused.",
}


class OptionsLevelView(BaseModel):
    level: ApprovalLevel
    allows: str


class OptionsLiveView(BaseModel):
    """A portfolio's live options state. ``allowed`` is true only when the
    switch is on, the stage is ``live_small`` or higher and the level is
    above ``none``."""

    portfolio_id: str
    enabled: bool = Field(description="[production.options] live, set by the admin")
    stage: str | None
    level: ApprovalLevel
    allowed: bool
    reasons: list[str]
    reason: str | None = Field(None, description="why the level was set")
    updated_at: datetime | None = None
    updated_by: str | None = None
    levels: list[OptionsLevelView]
    auto_approve_closes: bool
    expiry_action: str
    close_sessions: int


class OptionsApprovalUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    level: ApprovalLevel
    reason: str = Field(min_length=1, max_length=500)


class OptionsLiveService:
    def __init__(self, context: AppContext) -> None:
        self._ctx = context

    def view(self, principal: Principal, portfolio_id: str) -> OptionsLiveView:
        require(principal, Permission.READ)
        with self._ctx.state() as state:
            self._owned(state, principal, portfolio_id)
            return self._view(state, portfolio_id)

    def set_level(
        self, principal: Principal, portfolio_id: str, body: OptionsApprovalUpdate
    ) -> OptionsLiveView:
        require(principal, Permission.LIVE_MANAGE)
        with self._ctx.state() as state:
            self._owned(state, principal, portfolio_id)
            try:
                set_approval(
                    state, portfolio_id, body.level, actor=principal.actor, reason=body.reason
                )
            except ApprovalError as exc:
                raise ValidationError(str(exc)) from exc
            return self._view(state, portfolio_id)

    def _view(self, state: SqliteState, portfolio_id: str) -> OptionsLiveView:
        settings = self._ctx.settings.production.options
        approval = get_approval(state, portfolio_id)
        stage = get_stage(state, portfolio_id) if stages_enabled(state) else None
        gate = options_live_state(settings.live, stage, approval.level)
        return OptionsLiveView(
            portfolio_id=portfolio_id,
            enabled=settings.live,
            stage=stage,
            level=approval.level,
            allowed=gate.allowed,
            reasons=list(gate.reasons),
            reason=approval.reason,
            updated_at=datetime.fromisoformat(approval.updated_at) if approval.updated_at else None,
            updated_by=approval.updated_by,
            levels=[OptionsLevelView(level=lv, allows=LEVEL_TEXT[lv]) for lv in LEVELS],
            auto_approve_closes=settings.auto_approve_closes,
            expiry_action=settings.expiry.action,
            close_sessions=settings.expiry.close_sessions,
        )

    @staticmethod
    def _owned(state: SqliteState, principal: Principal, portfolio_id: str) -> str:
        try:
            return owned_portfolio(state, principal.scope, portfolio_id).id
        except NotFound as exc:
            raise NotFoundError(str(exc)) from exc

"""SubscriptionService: your subscriptions to strategies, over the accounts
repository (design section 4, decision 2026-09-26).

- Every call is scoped to the caller. Another user's subscription or
  portfolio reads as missing (404).
- Subscribing and changing a subscription need ``portfolio.trade``.
- Switching to ``auto`` needs ``subscription.auto_enable``, which asks for
  a fresh second factor (403 ``step_up_required``), and then the auto gate:
  an active strategy, an active broker portfolio and at least
  ``MIN_PAPER_DAYS_FOR_AUTO`` paper trading days without a risk breach
  (409 ``auto_blocked`` with every blocker).
- Switching to ``approve`` (roadmap 19.8, optional: every order waits for
  a person) asks for the same step-up and checklist. So does approve to
  auto, which takes the person out of each order.
- Starting or restarting an approve or auto book first replays its
  strategy over the last sessions (roadmap 23.15, ``app.replay``): no
  decision at all, or every call failing, is a blocker. P&L never is.
- Turning a disabled or paused auto subscription back on (``enabled:
  true``) restarts real orders, so it asks for the same step-up and runs
  the same checklist (UX-02). A paused one is resumed. Turning it off
  needs neither.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from datetime import datetime
from typing import Any, ClassVar, Literal

from pydantic import BaseModel, ConfigDict, Field

from stonks.accounts import (
    MAX_WEIGHT,
    MIN_PAPER_DAYS_FOR_AUTO,
    AccountsError,
    AutoGateRefused,
    Mode,
    NotFound,
    Subscription,
    SubscriptionRepository,
)
from stonks.app.context import AppContext
from stonks.app.errors import ConflictError, NotFoundError, ValidationError
from stonks.auth.policy import Permission, require
from stonks.auth.principal import Principal
from stonks.store.state import SqliteState

ModeName = Literal["notify", "paper", "approve", "auto"]


class AutoBlocked(ConflictError):
    """The auto checklist failed; ``blockers`` lists every failing item."""

    code: ClassVar[str] = "auto_blocked"

    def __init__(self, blockers: list[str]) -> None:
        super().__init__("auto mode refused: " + "; ".join(blockers))
        self.blockers = list(blockers)

    def problem_extensions(self) -> dict[str, Any]:
        return {"blockers": self.blockers}


class SubscriptionView(BaseModel):
    id: str
    strategy_id: str
    #: The strategy's catalog status (``active``, ``shadow``, ``retired``).
    strategy_status: str | None
    portfolio_id: str | None
    mode: ModeName
    weight: float
    enabled: bool
    paper_days_completed: int
    #: Paper trading days needed before auto (decision 2026-09-26).
    paper_days_required: int
    #: Every auto checklist item that fails today (empty: auto may be switched on).
    auto_blockers: list[str]
    #: Set while an auto subscription is paused (broker error, kill switch, ...).
    paused_reason: str | None
    auto_enabled_at: datetime | None
    created_at: datetime
    updated_at: datetime


class SubscribeRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    strategy_id: str = Field(min_length=1, max_length=128)
    #: Needed for ``paper`` (one of your portfolios); optional for ``notify``.
    portfolio_id: str | None = Field(default=None, max_length=64)
    #: New subscriptions start in ``notify`` or ``paper``; ``auto`` is refused (409).
    mode: ModeName = "notify"
    #: The strategy's share of the portfolio's risk budget (finite, at most 100).
    weight: float = Field(default=1.0, ge=0, le=MAX_WEIGHT, allow_inf_nan=False)


class SubscriptionUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    enabled: bool | None = None
    mode: ModeName | None = None
    #: Written to the audit log with the change.
    reason: str | None = Field(default=None, max_length=500)


@contextmanager
def _errors() -> Iterator[None]:
    try:
        yield
    except NotFound as exc:
        raise NotFoundError(str(exc)) from None
    except AutoGateRefused as exc:
        raise AutoBlocked(exc.reasons) from None
    except AccountsError as exc:
        raise ValidationError(str(exc)) from None


class SubscriptionService:
    def __init__(self, context: AppContext) -> None:
        self._ctx = context

    def list(self, principal: Principal) -> list[SubscriptionView]:
        """Your subscriptions, oldest first."""
        require(principal, Permission.READ)
        with self._ctx.state() as state, _errors():
            repo = SubscriptionRepository(state)
            return [
                self._view(state, repo, principal, s) for s in repo.list_for_user(principal.scope)
            ]

    def subscribe(self, principal: Principal, request: SubscribeRequest) -> SubscriptionView:
        require(principal, Permission.PORTFOLIO_TRADE)
        with self._ctx.state() as state, _errors():
            repo = SubscriptionRepository(state)
            sub = repo.subscribe(
                principal.scope,
                strategy_id=request.strategy_id,
                mode=Mode(request.mode),
                portfolio_id=request.portfolio_id,
                weight=request.weight,
            )
            return self._view(state, repo, principal, sub)

    def update(
        self, principal: Principal, subscription_id: str, request: SubscriptionUpdate
    ) -> SubscriptionView:
        """Change the mode and/or turn the subscription on or off. The mode
        change runs first, so a refused switch to auto changes nothing."""
        require(principal, Permission.PORTFOLIO_TRADE)
        scope = principal.scope
        with self._ctx.state() as state, _errors():
            repo = SubscriptionRepository(state)
            sub = repo.get(scope, subscription_id)
            if request.mode is not None:
                mode = Mode(request.mode)
                if mode.trades_live and (mode is not sub.mode or sub.auto_paused):
                    require(principal, Permission.AUTO_ENABLE)
                    if not repo.auto_blockers(scope, sub.id):  # else set_mode refuses first
                        self._check_replay(state, sub)
                sub = repo.set_mode(scope, sub.id, mode, reason=request.reason)
            restarts = (
                request.enabled is True
                and sub.mode.trades_live
                and (not sub.enabled or sub.auto_paused)
            )
            if restarts:
                # UX-02: back on in auto means real orders again: the same
                # step-up and checklist as switching to auto.
                require(principal, Permission.AUTO_ENABLE)
                blockers = repo.auto_blockers(scope, sub.id)
                if blockers:
                    raise AutoBlocked(blockers)
                self._check_replay(state, sub)
                if sub.auto_paused:
                    sub = repo.set_mode(scope, sub.id, sub.mode, reason=request.reason)
            if request.enabled is not None:
                change = repo.enable if request.enabled else repo.disable
                sub = change(scope, sub.id, reason=request.reason)
            return self._view(state, repo, principal, sub)

    def _check_replay(self, state: SqliteState, sub: Subscription) -> None:
        """Roadmap 23.15: an approve or auto book that starts or restarts
        first replays its strategy over the last sessions. No decision at
        all, or every call failing, refuses it. Recent P&L never does."""
        from stonks.app.replay import run_replay

        report = run_replay(self._ctx, state, [sub.strategy_id])
        if report.passed is False:
            raise AutoBlocked([f"recent replay: {b}" for b in report.blockers()])

    @staticmethod
    def _view(
        state: SqliteState, repo: SubscriptionRepository, principal: Principal, s: Subscription
    ) -> SubscriptionView:
        rows = state.sql("SELECT status FROM strategies WHERE id = ?", [s.strategy_id])
        blockers = [] if s.mode.trades_live and not s.auto_paused else None
        if blockers is None:
            blockers = repo.auto_blockers(principal.scope, s.id)
        return SubscriptionView(
            id=s.id,
            strategy_id=s.strategy_id,
            strategy_status=rows[0]["status"] if rows else None,
            portfolio_id=s.portfolio_id,
            mode=s.mode.value,
            weight=s.weight,
            enabled=s.enabled,
            paper_days_completed=s.paper_days_completed,
            paper_days_required=MIN_PAPER_DAYS_FOR_AUTO,
            auto_blockers=blockers,
            paused_reason=s.paused_reason,
            auto_enabled_at=datetime.fromisoformat(s.auto_enabled_at)
            if s.auto_enabled_at
            else None,
            created_at=datetime.fromisoformat(s.created_at),
            updated_at=datetime.fromisoformat(s.updated_at),
        )

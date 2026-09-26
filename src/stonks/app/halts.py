"""HaltService: the kill switch and risk halts for the CLI, the API and MCP
(roadmap 12.6, BL-28; design section 9).

The kill switch is a ``risk_halts`` row of kind ``kill`` at one of three
scopes:

- ``global``: every portfolio. Admins only;
- ``user``: every portfolio of the caller;
- ``portfolio``: one portfolio the caller owns.

It stops every new order (``halt = all``), or only buys when ``flatten``
is set so sells and exits still go through. The tick's ``risk_halts`` gate
enforces it. Engaging, resuming and clearing each write an ``audit_log``
row; resuming and clearing also write the ``risk_reset`` row in
``status_changes``. Resuming the kill switch needs the typed confirmation
:data:`RESUME_PHRASE` and a reason. Other halts (the circuit breaker, the
operational halt) are cleared with a reason by the portfolio owner, or an
admin for global ones.

Other users' portfolios and halts read as missing (404), never forbidden.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, date, datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from stonks.accounts import NotFound, Role, Scope, owned_portfolio
from stonks.accounts.audit import AuditLog
from stonks.app.context import AppContext
from stonks.app.errors import NotFoundError, ValidationError
from stonks.auth.errors import PermissionDenied
from stonks.auth.policy import Permission, require
from stonks.auth.principal import Principal
from stonks.logging import get_logger
from stonks.production.halts import (
    Halt,
    HaltError,
    clear_halt,
    get_halt,
    list_halts,
    notify_trip,
    trip_halt,
)
from stonks.store.state import SqliteState

_log = get_logger("stonks.app.halts")

#: What a person types to resume trading after the kill switch.
RESUME_PHRASE = "RESUME TRADING"

KillScope = Literal["global", "user", "portfolio"]


# ---- requests and views ----------------------------------------------------------------


class KillSwitchRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    scope: KillScope
    #: Required with ``scope = "portfolio"``; a portfolio you own.
    portfolio_id: str | None = Field(default=None, max_length=64)
    #: Stop buys only and let sells and exits through.
    flatten: bool = False
    reason: str = Field(min_length=1, max_length=500)


class ResumeRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    #: Must be exactly ``RESUME TRADING``.
    confirmation: str = Field(max_length=64)
    reason: str = Field(min_length=1, max_length=500)


class ClearHaltRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    reason: str = Field(min_length=1, max_length=500)


class HaltView(BaseModel):
    id: int
    kind: Literal["month_loss", "week_loss", "drawdown", "operational", "kill"]
    scope: KillScope
    user_id: str | None
    portfolio_id: str | None
    #: ``all``: no new orders; ``buys``: sells and exits still go through.
    halt: Literal["buys", "all"]
    reason: str
    tripped_by: str
    tripped_at: str
    #: The first day it no longer applies; null until cleared.
    expires_on: date | None
    cleared_at: str | None
    cleared_by: str | None
    clear_reason: str | None
    #: In force today.
    active: bool

    @classmethod
    def of(cls, h: Halt, today: date) -> HaltView:
        return cls(
            id=h.id,
            kind=h.kind,
            scope=h.scope,
            user_id=h.user_id,
            portfolio_id=h.portfolio_id,
            halt=h.halt,
            reason=h.reason,
            tripped_by=h.tripped_by,
            tripped_at=h.tripped_at,
            expires_on=h.expires_on,
            cleared_at=h.cleared_at,
            cleared_by=h.cleared_by,
            clear_reason=h.clear_reason,
            active=h.active_on(today),
        )


def _today() -> date:
    return datetime.now(UTC).date()


#: The caller: a :class:`Principal` (API, MCP) or a bare :class:`Scope`
#: (the CLI and in-process services).
Who = Scope | Principal


def _scope(who: Who) -> Scope:
    return who.scope if isinstance(who, Principal) else who


def _is_admin(scope: Scope) -> bool:
    return scope.is_service or scope.role == Role.ADMIN


def _check(who: Who, permission: Permission, allowed: bool, message: str) -> None:
    """A principal needs ``permission`` (role *and* credential scope); a bare
    scope (CLI, services) needs ``allowed``. Refusals are 403."""
    if isinstance(who, Principal):
        require(who, permission)
    elif not allowed:
        raise PermissionDenied(message)


class HaltService:
    def __init__(self, context: AppContext) -> None:
        self._context = context

    @contextmanager
    def _state(self) -> Iterator[SqliteState]:
        with self._context.state() as state:
            yield state

    # ---- reads -----------------------------------------------------------------------

    def list(self, who: Who, *, include_cleared: bool = False) -> list[HaltView]:
        """Halts the caller can see, newest first: global ones, their own
        user halts and those of portfolios they own. By default only the
        halts in force today."""
        scope = _scope(who)
        today = _today()
        with self._state() as state:
            halts = list_halts(state, include_cleared=include_cleared, on=today)
            return [HaltView.of(h, today) for h in halts if self._visible(state, scope, h)]

    def get(self, who: Who, halt_id: int) -> HaltView:
        scope = _scope(who)
        with self._state() as state:
            return HaltView.of(self._load(state, scope, halt_id), _today())

    # ---- the kill switch ---------------------------------------------------------------

    def engage_kill(
        self, who: Who, request: KillSwitchRequest, *, ip: str | None = None
    ) -> HaltView:
        """Stop new orders at ``request.scope``. Idempotent: a kill switch
        already on at that scope is returned as it is."""
        scope = _scope(who)
        _check(
            who,
            Permission.KILLSWITCH_USER,
            scope.is_service or Role(scope.role).can_trade,
            "the kill switch needs a role that can trade",
        )
        user_id: str | None = None
        portfolio_id: str | None = None
        with self._state() as state:
            if request.scope == "global":
                _check(
                    who,
                    Permission.KILLSWITCH_GLOBAL,
                    _is_admin(scope),
                    "only an admin can stop every portfolio",
                )
            elif request.scope == "user":
                if scope.is_service:
                    raise ValidationError("a service has no portfolios of its own")
                user_id = scope.user_id
            else:
                if not request.portfolio_id:
                    raise ValidationError("a portfolio kill switch needs portfolio_id")
                portfolio_id = self._owned(state, scope, request.portfolio_id)
            with state.transaction():
                halt, created = trip_halt(
                    state,
                    "kill",
                    reason=request.reason,
                    actor=scope.actor,
                    scope=request.scope,
                    user_id=user_id,
                    portfolio_id=portfolio_id,
                    halt="buys" if request.flatten else "all",
                    on=_today(),
                )
                if created:
                    AuditLog(state).record(
                        scope.actor,
                        "kill_switch.engage",
                        "risk_halt",
                        str(halt.id),
                        portfolio_id=portfolio_id,
                        details={
                            "scope": request.scope,
                            "user_id": user_id,
                            "flatten": request.flatten,
                            "reason": request.reason,
                        },
                        ip=ip,
                    )
            if created:
                notify_trip(state, halt)
            _log.warning(
                "kill_switch.engaged",
                halt_id=halt.id,
                scope=request.scope,
                created=created,
                actor=scope.actor,
            )
            return HaltView.of(halt, _today())

    def resume_kill(
        self, who: Who, halt_id: int, request: ResumeRequest, *, ip: str | None = None
    ) -> HaltView:
        """Turn a kill switch off. Needs :data:`RESUME_PHRASE` typed exactly.

        A person needs a second factor checked in the last few minutes
        (step-up), so API tokens are refused (``StepUpRequired``). A bare
        :class:`Scope` is the CLI, where shell access already implies admin."""
        if isinstance(who, Principal):
            require(who, Permission.KILLSWITCH_RESUME)
        scope = _scope(who)
        if request.confirmation.strip() != RESUME_PHRASE:
            raise ValidationError(f"type {RESUME_PHRASE!r} to resume trading")
        with self._state() as state:
            halt = self._load(state, scope, halt_id)
            if halt.kind != "kill":
                raise ValidationError(f"halt {halt_id} is a {halt.kind} halt; clear it instead")
            return self._clear(state, who, halt, request.reason, "kill_switch.resume", ip)

    # ---- other halts -------------------------------------------------------------------

    def clear(
        self, who: Who, halt_id: int, request: ClearHaltRequest, *, ip: str | None = None
    ) -> HaltView:
        """The logged reset of a circuit-breaker or operational halt."""
        scope = _scope(who)
        with self._state() as state:
            halt = self._load(state, scope, halt_id)
            if halt.kind == "kill":
                raise ValidationError("the kill switch is turned off with resume")
            return self._clear(state, who, halt, request.reason, "risk_halt.clear", ip)

    # ---- helpers -----------------------------------------------------------------------

    def _clear(
        self,
        state: SqliteState,
        who: Who,
        halt: Halt,
        reason: str,
        action: str,
        ip: str | None,
    ) -> HaltView:
        scope = _scope(who)
        if halt.scope == "global":
            _check(
                who,
                Permission.KILLSWITCH_GLOBAL if halt.kind == "kill" else Permission.RISK_GLOBAL,
                _is_admin(scope),
                "only an admin can clear a global halt",
            )
        if halt.cleared:
            raise ValidationError(f"halt {halt.id} was already cleared")
        try:
            with state.transaction():
                cleared = clear_halt(state, halt.id, actor=scope.actor, reason=reason)
                AuditLog(state).record(
                    scope.actor,
                    action,
                    "risk_halt",
                    str(halt.id),
                    portfolio_id=halt.portfolio_id,
                    details={"kind": halt.kind, "scope": halt.scope, "reason": reason},
                    ip=ip,
                )
        except HaltError as exc:
            raise ValidationError(str(exc)) from exc
        return HaltView.of(cleared, _today())

    def _owned(self, state: SqliteState, scope: Scope, portfolio_id: str) -> str:
        try:
            return owned_portfolio(state, scope, portfolio_id).id
        except NotFound as exc:
            raise NotFoundError(str(exc)) from exc

    def _visible(self, state: SqliteState, scope: Scope, halt: Halt) -> bool:
        if halt.scope == "global" or scope.is_service:
            return True
        if halt.scope == "user":
            return halt.user_id == scope.user_id
        try:
            owned_portfolio(state, scope, halt.portfolio_id or "")
        except NotFound:
            return False
        return True

    def _load(self, state: SqliteState, scope: Scope, halt_id: int) -> Halt:
        try:
            halt = get_halt(state, halt_id)
        except HaltError:
            halt = None
        if halt is None or not self._visible(state, scope, halt):
            raise NotFoundError(f"halt {halt_id} not found")
        return halt

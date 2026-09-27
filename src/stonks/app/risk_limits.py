"""RiskLimitsService: a trader's own risk limits (roadmap 13.8).

The system policy (``[production.risk]``) applies to every book. A trader
may add limits of their own (``users.risk_policy_json``) that tighten every
portfolio they own: the tick merges them with
:func:`stonks.accounts.book.tighter_of`, so a limit can only ever make the
system one stricter (P28). This service reads and replaces them.

- Reading needs ``data.read``, replacing needs ``portfolio.manage``. Both
  act on the caller only.
- Values are validated as a partial ``RiskPolicy`` (bad values and unknown
  keys are a 422). A value looser than the system limit is stored but has no
  effect, and ``ignored`` names it so the console can say so.
- Every change writes an ``audit_log`` row (``user.risk_policy``).
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict
from pydantic import ValidationError as PydanticValidationError

from stonks.accounts.book import partial_risk_policy, tighter_of
from stonks.accounts.models import NotFound
from stonks.accounts.users import UserRepository
from stonks.app.context import AppContext
from stonks.app.errors import NotFoundError, ValidationError
from stonks.auth.policy import Permission, require
from stonks.auth.principal import Principal
from stonks.config import RiskPolicy


class RiskLimitsView(BaseModel):
    #: The system policy every book follows.
    system: RiskPolicy
    #: Your own limits: only the fields you set.
    mine: dict[str, Any]
    #: What your portfolios follow: the system policy tightened by yours
    #: (a portfolio's own limits can tighten it further).
    effective: RiskPolicy
    #: Fields you set that are looser than the system, so they change nothing.
    ignored: list[str]


class RiskLimitsUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    #: A partial ``RiskPolicy``: the fields to limit. ``{}`` clears them all.
    limits: dict[str, Any]


class RiskLimitsService:
    def __init__(self, context: AppContext) -> None:
        self._ctx = context

    def get(self, principal: Principal) -> RiskLimitsView:
        require(principal, Permission.READ)
        with self._ctx.state() as state:
            try:
                mine = UserRepository(state).risk_policy(self._person(principal))
            except NotFound as exc:
                raise NotFoundError(str(exc)) from None
        return self._view(mine)

    def set(self, principal: Principal, body: RiskLimitsUpdate) -> RiskLimitsView:
        require(principal, Permission.PORTFOLIO_MANAGE)
        user_id = self._person(principal)
        try:
            partial_risk_policy(body.limits)
        except PydanticValidationError as exc:
            raise ValidationError(_first_error(exc)) from None
        with self._ctx.state() as state:
            try:
                stored = UserRepository(state).set_risk_policy(
                    user_id, body.limits, actor=principal.actor
                )
            except NotFound as exc:
                raise NotFoundError(str(exc)) from None
        return self._view(stored)

    def _view(self, mine: dict[str, Any]) -> RiskLimitsView:
        system = self._ctx.settings.production.risk.model_copy(deep=True)
        effective = tighter_of(system, mine)
        ignored = sorted(
            k
            for k, v in mine.items()
            if getattr(effective, k) == getattr(system, k)
            and getattr(RiskPolicy.model_validate({k: v}), k) != getattr(system, k)
        )
        return RiskLimitsView(system=system, mine=mine, effective=effective, ignored=ignored)

    @staticmethod
    def _person(principal: Principal) -> str:
        if principal.scope.is_service:
            raise ValidationError("risk limits belong to a person, not a service")
        return principal.user_id


def _first_error(exc: PydanticValidationError) -> str:
    err = exc.errors()[0]
    where = ".".join(str(p) for p in err.get("loc", ())) or "limits"
    return f"{where}: {err.get('msg', 'invalid value')}"

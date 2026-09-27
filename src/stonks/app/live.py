"""LiveService: the owner's settings of a live portfolio (roadmap 19.6, 19.7).

- **Allocation**: how much Stonks may trade in the portfolio. Set by hand
  only. There are no automatic steps or suggestions, and a bad week never
  changes it.
- **Account profile**: where the broker account is held (US, EU or UK),
  cash or margin, retail or professional. It picks the account rules.

Reading needs ``data.read`` and the portfolio (another person's reads as
missing, admins included). Changing either needs ``live.manage``: a
trader with a fresh second factor in the web app (403
``step_up_required`` otherwise, and always for API tokens). Every change
writes an ``audit_log`` row.
"""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from stonks.accounts import NotFound, owned_portfolio
from stonks.accounts.rules import AccountProfile
from stonks.accounts.rules.profiles import ProfileError, get_profile, set_profile
from stonks.app.context import AppContext
from stonks.app.errors import NotFoundError, ValidationError
from stonks.auth.policy import Permission, require
from stonks.auth.principal import Principal
from stonks.production.live.allocation import AllocationError, get_allocation, set_allocation
from stonks.store.state import SqliteState


class LiveAllocationView(BaseModel):
    portfolio_id: str
    #: ``None``: never set, so nothing may open in the live book.
    amount: float | None
    currency: str | None
    reason: str | None
    updated_at: datetime | None
    updated_by: str | None


class LiveAllocationUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    amount: float = Field(ge=0, allow_inf_nan=False)
    currency: str = Field(min_length=3, max_length=3)
    reason: str = Field(min_length=1, max_length=500)

    @field_validator("currency")
    @classmethod
    def _upper(cls, v: str) -> str:
        return v.strip().upper()


class AccountProfileBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    jurisdiction: Literal["us", "eu", "uk"]
    account_type: Literal["cash", "margin"] = "cash"
    client_class: Literal["retail", "professional"] = "retail"
    base_currency: str = Field(default="USD", min_length=3, max_length=3)
    fx_policy: Literal["refuse", "convert"] = "refuse"
    wash_sale_mode: Literal["warn", "block"] = "warn"
    allow_short: bool = False

    @field_validator("base_currency")
    @classmethod
    def _upper(cls, v: str) -> str:
        return v.strip().upper()


class AccountProfileView(AccountProfileBody):
    portfolio_id: str


class LiveService:
    def __init__(self, context: AppContext) -> None:
        self._ctx = context

    def get_allocation(self, principal: Principal, portfolio_id: str) -> LiveAllocationView:
        require(principal, Permission.READ)
        with self._ctx.state() as state:
            self._owned(state, principal, portfolio_id)
            got = get_allocation(state, portfolio_id)
        if got is None:
            return LiveAllocationView(
                portfolio_id=portfolio_id,
                amount=None,
                currency=None,
                reason=None,
                updated_at=None,
                updated_by=None,
            )
        return LiveAllocationView(
            portfolio_id=portfolio_id,
            amount=got.amount,
            currency=got.currency,
            reason=got.reason,
            updated_at=datetime.fromisoformat(got.updated_at),
            updated_by=got.updated_by,
        )

    def set_allocation(
        self, principal: Principal, portfolio_id: str, body: LiveAllocationUpdate
    ) -> LiveAllocationView:
        require(principal, Permission.LIVE_MANAGE)
        with self._ctx.state() as state:
            self._owned(state, principal, portfolio_id)
            try:
                set_allocation(
                    state,
                    portfolio_id,
                    body.amount,
                    currency=body.currency,
                    actor=principal.actor,
                    reason=body.reason,
                )
            except AllocationError as exc:
                raise ValidationError(str(exc)) from exc
        return self.get_allocation(principal, portfolio_id)

    def get_profile(self, principal: Principal, portfolio_id: str) -> AccountProfileView:
        require(principal, Permission.READ)
        with self._ctx.state() as state:
            self._owned(state, principal, portfolio_id)
            got = get_profile(state, portfolio_id)
        if got is None:
            raise NotFoundError(f"portfolio {portfolio_id!r} has no account profile")
        return _profile_view(got)

    def set_profile(
        self, principal: Principal, portfolio_id: str, body: AccountProfileBody
    ) -> AccountProfileView:
        require(principal, Permission.LIVE_MANAGE)
        profile = AccountProfile(portfolio_id=portfolio_id, **body.model_dump())
        with self._ctx.state() as state:
            self._owned(state, principal, portfolio_id)
            try:
                saved = set_profile(state, profile, actor=principal.actor)
            except ProfileError as exc:
                raise ValidationError(str(exc)) from exc
        return _profile_view(saved)

    @staticmethod
    def _owned(state: SqliteState, principal: Principal, portfolio_id: str) -> str:
        try:
            return owned_portfolio(state, principal.scope, portfolio_id).id
        except NotFound as exc:
            raise NotFoundError(str(exc)) from exc


def _profile_view(p: AccountProfile) -> AccountProfileView:
    return AccountProfileView(
        portfolio_id=p.portfolio_id,
        jurisdiction=p.jurisdiction,
        account_type=p.account_type,
        client_class=p.client_class,
        base_currency=p.base_currency,
        fx_policy=p.fx_policy,
        wash_sale_mode=p.wash_sale_mode,
        allow_short=p.allow_short,
    )

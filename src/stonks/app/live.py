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

import json
from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from stonks.accounts import NotFound, owned_portfolio, tighter_of
from stonks.accounts.models import Portfolio
from stonks.accounts.rules import AccountProfile, registered_account_rules
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


#: The live safeguards and protections, in the order the tick runs them.
LIVE_RULES: tuple[str, ...] = (
    "capital_ramp",
    "live_notional_caps",
    "price_band",
    "account_rules",
    "max_orders_per_run",
    "stop_cooldown",
    "stop_guard",
    "losing_lock",
)


class LiveRuleView(BaseModel):
    #: The risk rule's name (``capital_ramp``, ``price_band``, ...).
    name: str
    #: Whether the rule acts on this portfolio's live book.
    on: bool
    #: The rule's settings as this portfolio follows them.
    settings: dict[str, Any]


class AccountRuleView(BaseModel):
    #: The account rule's name (``settled_cash``, ``pdt``, ...).
    name: str
    #: Whether it applies to the portfolio's account profile (``false``
    #: while no profile is set).
    applies: bool


class LiveRulesView(BaseModel):
    portfolio_id: str
    #: Every live safeguard and protection with its state.
    safeguards: list[LiveRuleView]
    #: Whether the account rules engine is on for this portfolio.
    account_rules_on: bool
    #: Whether the owner set an account profile. Without one, a live book
    #: with the account rules on opens nothing.
    profile_set: bool
    #: Every account rule and whether it applies to the profile.
    account_rules: list[AccountRuleView]


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

    def rules(self, principal: Principal, portfolio_id: str) -> LiveRulesView:
        """Which live safeguards and account rules act on this portfolio,
        read from the policy its book follows (the system policy tightened
        by the owner's limits and the portfolio's own). Read only."""
        require(principal, Permission.READ)
        with self._ctx.state() as state:
            portfolio = self._portfolio(state, principal, portfolio_id)
            owner = state.sql(
                "SELECT risk_policy_json FROM users WHERE id = ?", [portfolio.owner_id]
            )
            profile = get_profile(state, portfolio_id)
        owner_risk = json.loads(owner[0]["risk_policy_json"] or "{}") if owner else {}
        policy = tighter_of(
            self._ctx.settings.production.risk, owner_risk or None, portfolio.risk_policy
        )
        safeguards = [
            LiveRuleView(
                name=name,
                on=bool(policy.enabled and getattr(policy.rules, name).active),
                settings=getattr(policy.rules, name).model_dump(mode="json"),
            )
            for name in LIVE_RULES
        ]
        applying = (
            {r.name for r in registered_account_rules(profile)} if profile is not None else set()
        )
        return LiveRulesView(
            portfolio_id=portfolio_id,
            safeguards=safeguards,
            account_rules_on=next(r.on for r in safeguards if r.name == "account_rules"),
            profile_set=profile is not None,
            account_rules=[
                AccountRuleView(name=r.name, applies=r.name in applying)
                for r in registered_account_rules()
            ],
        )

    @staticmethod
    def _portfolio(state: SqliteState, principal: Principal, portfolio_id: str) -> Portfolio:
        try:
            return owned_portfolio(state, principal.scope, portfolio_id)
        except NotFound as exc:
            raise NotFoundError(str(exc)) from exc

    @classmethod
    def _owned(cls, state: SqliteState, principal: Principal, portfolio_id: str) -> str:
        return cls._portfolio(state, principal, portfolio_id).id


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

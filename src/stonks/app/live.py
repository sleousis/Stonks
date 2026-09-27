"""LiveService: the owner's settings of a live portfolio (roadmap 19.6, 19.7, 19.9).

- **Allocation**: how much Stonks may trade in the portfolio. Set by hand
  only. There are no automatic steps or suggestions, and a bad week never
  changes it.
- **Account profile**: where the broker account is held (US, EU or UK),
  cash or margin, retail or professional. It picks the account rules. It
  is locked while the portfolio trades real money (``live_small`` or up).
- **Stage** (19.9): ``sim_paper``, ``broker_paper``, ``live_small``,
  ``live_scale``. A promotion goes one stage up with a passing gate report
  computed now, a typed confirmation and ``live.manage`` (a fresh second
  factor). A demotion goes down with a reason and ``portfolio.trade``.
- **Preview** (19.9): a dry run of the live book through the broker's
  what-if. It never transmits, so it needs ``portfolio.trade`` only.

Reading needs ``data.read`` and the portfolio (another person's reads as
missing, admins included). Changing either needs ``live.manage``: a
trader with a fresh second factor in the web app (403
``step_up_required`` otherwise, and always for API tokens). Every change
writes an ``audit_log`` row.
"""

from __future__ import annotations

import json
from datetime import date, datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from stonks.accounts import NotFound, owned_portfolio, tighter_of
from stonks.accounts.models import DEFAULT_PORTFOLIO_ID, Portfolio
from stonks.accounts.rules import AccountProfile, registered_account_rules
from stonks.accounts.rules.profiles import ProfileError, get_profile, set_profile
from stonks.app.context import AppContext
from stonks.app.errors import ConflictError, NotFoundError, ValidationError
from stonks.auth.policy import Permission, require
from stonks.auth.principal import Principal
from stonks.production.live.allocation import AllocationError, get_allocation, set_allocation
from stonks.production.live.gates import GateFacts, GateReport, gate_days, gate_report
from stonks.production.live.preview import LivePreview, PreviewError, live_book, run_preview
from stonks.production.live.stages import (
    LiveStage,
    StageError,
    change_stage,
    get_stage,
    next_stage,
    stage_history,
    stage_index,
    trades_real_money,
)
from stonks.production.rules import RiskAdjustment
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
    "protective_stops",
)
#: Live settings that are not risk rules, so the risk layer's own on and off
#: does not apply: ``protective_stops`` places stops and drops no order.
_NOT_RULES = frozenset({"protective_stops"})


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


class GateDayView(BaseModel):
    session_date: date
    stage: LiveStage
    orders_sent: int
    orders_filled: int
    orders_rejected: int
    #: Orders our own rules dropped (not failures).
    orders_refused: int
    stuck_orders: int
    fills: int
    fills_missing_commission: int
    tca_orders: int
    #: Realised shortfall minus the cost model's estimate, in bps.
    tca_gap_bps: float | None
    live_return: float | None
    model_return: float | None
    #: ``None``: no reconcile report to read that session.
    drift_items: int | None
    reject_rate: float
    clean: bool


class StageChangeView(BaseModel):
    id: int
    from_stage: LiveStage
    to_stage: LiveStage
    direction: Literal["promote", "demote"]
    actor: str
    reason: str
    #: The gate report the promotion passed (``None`` for a demotion).
    gate_report: dict[str, Any] | None
    created_at: datetime


class LiveStageView(BaseModel):
    portfolio_id: str
    stage: LiveStage
    #: Where a promotion leads (``None`` at the top).
    next_stage: LiveStage | None
    #: Whether this stage trades real money.
    real_money: bool
    #: The newest changes first.
    history: list[StageChangeView]
    #: The last sessions' gate metrics, oldest first.
    days: list[GateDayView]


class GateCheckView(BaseModel):
    name: str
    #: ``None``: the data does not exist yet. Shown, and not blocking.
    passed: bool | None
    detail: str
    value: Any = None
    required: Any = None


class GateReportView(BaseModel):
    portfolio_id: str
    from_stage: LiveStage
    #: The stage a promotion leads to (``None`` at the top).
    target: LiveStage | None
    passed: bool
    checks: list[GateCheckView]
    metrics: dict[str, Any]
    computed_at: datetime


class StagePromoteBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    to_stage: LiveStage
    reason: str = Field(min_length=1, max_length=500)
    #: Type the target stage's name again to confirm.
    confirm: str = Field(min_length=1, max_length=40)


class StageDemoteBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    to_stage: LiveStage
    reason: str = Field(min_length=1, max_length=500)


class WhatIfView(BaseModel):
    initial_margin_change: float
    maintenance_margin_change: float
    equity_with_loan_after: float
    commission: float | None
    commission_currency: str | None
    #: The broker refused the order in the what-if (its text).
    warning: str | None


class AdjustmentView(BaseModel):
    ticker: str
    side: str
    rule: str
    original_quantity: float
    adjusted_quantity: float
    reason: str


class PreviewOrderView(BaseModel):
    client_id: str
    ticker: str
    side: str
    quantity: float
    order_type: str
    limit_price: float | None
    time_in_force: str | None
    position_effect: str | None
    strategy_id: str | None
    notional: float | None
    what_if: WhatIfView | None
    #: Why the what-if failed. A failed what-if never lets a buy through.
    what_if_error: str | None
    adjustments: list[AdjustmentView]


class PreviewAccountView(BaseModel):
    equity: float
    cash: float
    settled_cash: float
    available_funds: float
    buying_power: float
    currency: str
    account_type: str


class LivePreviewView(BaseModel):
    portfolio_id: str
    as_of: date
    stage: LiveStage
    #: The live book's outcome in the dry run (``ok``, ``noop``, ...).
    status: str
    #: Why the book decided nothing, when it did.
    reason: str | None
    orders: list[PreviewOrderView]
    #: Every rule adjustment, dropped orders included.
    adjustments: list[AdjustmentView]
    #: The account as the broker reports it (``None``: not read).
    account: PreviewAccountView | None
    allocation: float | None
    what_if_available: bool
    #: Always false: a preview never sends an order.
    transmitted: bool
    notes: list[str]


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
            stage = get_stage(state, portfolio_id)
            if trades_real_money(stage):
                raise ConflictError(
                    "the account profile is locked while the portfolio trades real money"
                    f" ({stage}): demote it to broker_paper to change it"
                )
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
                on=bool(
                    (policy.enabled or name in _NOT_RULES) and getattr(policy.rules, name).active
                ),
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

    # ---- stages, gates and the preview (19.9) -------------------------------------

    def stage(self, principal: Principal, portfolio_id: str, days: int = 30) -> LiveStageView:
        require(principal, Permission.READ)
        with self._ctx.state() as state:
            self._owned(state, principal, portfolio_id)
            return _stage_view(state, portfolio_id, days)

    def gate_report(self, principal: Principal, portfolio_id: str) -> GateReportView:
        """What a promotion to the next stage needs, checked now."""
        require(principal, Permission.READ)
        with self._ctx.state() as state:
            portfolio = self._portfolio(state, principal, portfolio_id)
            report = self._report(state, portfolio)
        return GateReportView.model_validate(report.as_dict())

    def promote(
        self, principal: Principal, portfolio_id: str, body: StagePromoteBody
    ) -> LiveStageView:
        """One stage up. The gate report is computed now and must pass."""
        require(principal, Permission.LIVE_MANAGE)
        return self._promote(principal, principal.actor, portfolio_id, body)

    def promote_from_shell(
        self, principal: Principal, portfolio_id: str, body: StagePromoteBody, *, actor: str
    ) -> LiveStageView:
        """``stonks live stage promote``: the operator's shell, trusted like
        ``stonks users``. The typed confirmation stands in for the second
        factor. The gate report must still pass."""
        require(principal, Permission.PORTFOLIO_TRADE)
        return self._promote(principal, actor, portfolio_id, body)

    def _promote(
        self, principal: Principal, actor: str, portfolio_id: str, body: StagePromoteBody
    ) -> LiveStageView:
        if body.confirm.strip() != body.to_stage:
            raise ValidationError(f"type {body.to_stage} to confirm moving up")
        with self._ctx.state() as state:
            portfolio = self._portfolio(state, principal, portfolio_id)
            report = self._report(state, portfolio)
            if report.target != body.to_stage:
                raise ConflictError(
                    f"the portfolio is in {report.from_stage}: moving up leads to"
                    f" {report.target or 'nothing (the top stage)'}"
                )
            if not report.passed:
                failed = ", ".join(c.name for c in report.checks if c.passed is False)
                raise ConflictError(f"the gate to {body.to_stage} did not pass: {failed}")
            try:
                change_stage(
                    state,
                    portfolio_id,
                    body.to_stage,
                    actor=actor,
                    reason=body.reason,
                    gate_report=report.as_dict(),
                )
            except StageError as exc:
                raise ConflictError(str(exc)) from exc
            return _stage_view(state, portfolio_id, 30)

    def demote(
        self,
        principal: Principal,
        portfolio_id: str,
        body: StageDemoteBody,
        *,
        actor: str | None = None,
    ) -> LiveStageView:
        """Down any number of stages. It only reduces risk, so it needs no
        second factor and no report. ``actor``: the shell's own name."""
        require(principal, Permission.PORTFOLIO_TRADE)
        with self._ctx.state() as state:
            self._owned(state, principal, portfolio_id)
            current = get_stage(state, portfolio_id)
            if stage_index(body.to_stage) >= stage_index(current):
                raise ConflictError(f"the portfolio is in {current}: demote to a lower stage")
            try:
                change_stage(
                    state,
                    portfolio_id,
                    body.to_stage,
                    actor=actor or principal.actor,
                    reason=body.reason,
                )
            except StageError as exc:
                raise ConflictError(str(exc)) from exc
            return _stage_view(state, portfolio_id, 30)

    def preview(self, principal: Principal, portfolio_id: str) -> LivePreviewView:
        """The live book's decision as a dry run, through the broker's
        what-if. Nothing is sent and nothing is booked."""
        from stonks.app.ticks import TickRequest, request_universe
        from stonks.production.settings_builder import build_tick_runtime, connection_traders
        from stonks.production.tick import TickPlan, load_tick_plan

        require(principal, Permission.PORTFOLIO_TRADE)
        settings = self._ctx.settings
        with self._ctx.state() as state, self._ctx.lake() as lake:
            self._owned(state, principal, portfolio_id)
            # the books first (they do not depend on the universe), so a
            # portfolio with no live book is told so before any scoring
            books = build_tick_runtime(settings, [])
            if books.books_from_subscriptions:
                plan = load_tick_plan(
                    state, books.settings, traders=connection_traders(state), dry_run=True
                )
            elif portfolio_id == DEFAULT_PORTFOLIO_ID:
                plan = TickPlan.default(books.settings)
            else:
                raise ValidationError("a preview needs [production] books_from_subscriptions")
            try:
                live_book(plan, portfolio_id)
            except PreviewError as exc:
                raise ValidationError(str(exc)) from exc
            universe = request_universe(settings, lake, TickRequest(dry_run=True))
            runtime = build_tick_runtime(settings, universe)
            try:
                result = run_preview(
                    state,
                    lake,
                    self._ctx.registry_on(state),
                    runtime.settings,
                    plan,
                    portfolio_id,
                    broker_factory=runtime.broker_factory,
                )
            except PreviewError as exc:
                raise ValidationError(str(exc)) from exc
        return _preview_view(result)

    def _report(self, state: SqliteState, portfolio: Portfolio) -> GateReport:
        settings = self._ctx.settings
        facts = GateFacts(
            broker_linked=portfolio.kind == "broker"
            or (portfolio.id == DEFAULT_PORTFOLIO_ID and settings.brokers.kind != "simulated"),
            allocation_set=get_allocation(state, portfolio.id) is not None,
            profile_set=get_profile(state, portfolio.id) is not None,
        )
        return gate_report(
            state, portfolio.id, facts=facts, settings=settings.production.live.stages
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


def _stage_view(state: SqliteState, portfolio_id: str, days: int) -> LiveStageView:
    stage = get_stage(state, portfolio_id)
    return LiveStageView(
        portfolio_id=portfolio_id,
        stage=stage,
        next_stage=next_stage(stage),
        real_money=trades_real_money(stage),
        history=[
            StageChangeView(
                id=c.id,
                from_stage=c.from_stage,
                to_stage=c.to_stage,
                direction=c.direction,
                actor=c.actor,
                reason=c.reason,
                gate_report=c.gate_report,
                created_at=datetime.fromisoformat(c.created_at),
            )
            for c in stage_history(state, portfolio_id, limit=50)
        ],
        days=[
            GateDayView.model_validate(g.as_dict())
            for g in gate_days(state, portfolio_id, limit=days)
        ],
    )


def _adjustment(a: RiskAdjustment) -> AdjustmentView:
    return AdjustmentView(
        ticker=a.ticker,
        side=a.side,
        rule=a.rule,
        original_quantity=a.original_quantity,
        adjusted_quantity=a.adjusted_quantity,
        reason=a.reason,
    )


def _preview_view(p: LivePreview) -> LivePreviewView:
    account = p.account
    orders: list[PreviewOrderView] = []
    for o in p.orders:
        w = o.what_if
        orders.append(
            PreviewOrderView(
                client_id=o.client_id,
                ticker=o.ticker,
                side=o.side,
                quantity=o.quantity,
                order_type=o.order_type,
                limit_price=o.limit_price,
                time_in_force=o.time_in_force,
                position_effect=o.position_effect,
                strategy_id=o.strategy_id,
                notional=o.notional,
                what_if=None
                if w is None
                else WhatIfView(
                    initial_margin_change=w.initial_margin_change,
                    maintenance_margin_change=w.maintenance_margin_change,
                    equity_with_loan_after=w.equity_with_loan_after,
                    commission=w.commission,
                    commission_currency=w.commission_currency,
                    warning=w.warning,
                ),
                what_if_error=o.what_if_error,
                adjustments=[_adjustment(a) for a in o.adjustments],
            )
        )
    return LivePreviewView(
        portfolio_id=p.portfolio_id,
        as_of=p.as_of,
        stage=p.stage,
        status=p.status,
        reason=p.reason,
        orders=orders,
        adjustments=[_adjustment(a) for a in p.adjustments],
        account=None
        if account is None
        else PreviewAccountView(
            equity=account.equity,
            cash=account.cash,
            settled_cash=account.settled_cash,
            available_funds=account.available_funds,
            buying_power=account.buying_power,
            currency=account.currency,
            account_type=account.account_type,
        ),
        allocation=p.allocation,
        what_if_available=p.what_if_available,
        transmitted=p.transmitted,
        notes=list(p.notes),
    )

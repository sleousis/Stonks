"""ExecutionService (roadmap 23.16): execution algos per portfolio and the
rebalancing planner, for the API, MCP and the CLI.

- Reading the algo catalog, a portfolio's algo settings, its parent orders
  and a rebalancing plan needs ``read`` on a portfolio you own. A plan
  writes and sends nothing.
- Changing a portfolio's algo setting needs ``portfolio.manage``.
- Confirming a plan needs ``portfolio.trade``: it writes one order ticket
  per trade, each waiting for approval (``hold = approve_mode``). Approving
  still needs a fresh second factor in the web app (or the typed phrase in
  the CLI), so MCP and API tokens can plan and confirm but never send.
- The CLI passes a bare :class:`Scope` instead of a principal.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable
from datetime import UTC, date, datetime
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from stonks.accounts import NotFound, Scope, owned_portfolio
from stonks.accounts import Portfolio as AccountPortfolio
from stonks.accounts.audit import AuditLog
from stonks.app.context import AppContext
from stonks.app.errors import NotFoundError, ValidationError
from stonks.auth.errors import PermissionDenied
from stonks.auth.policy import POLICY, Permission, require
from stonks.auth.principal import Principal
from stonks.backtest.costs import ExecAlgoAssumption
from stonks.core.types import Order
from stonks.execution.algos import AlgoParamsError, algo_names, get_algo
from stonks.execution.algos.settings import (
    AlgoSetting,
    clear_setting,
    list_settings,
    resolve_algo,
    set_setting,
)
from stonks.portfolio.planner import PlanError, RebalancePlan, plan_rebalance
from stonks.production.algo_slices import ParentView, list_parents
from stonks.store.state import SqliteState

Who = Principal | Scope
_TICKER = r"^[A-Za-z0-9._\-^=:]{1,40}$"
_ID = r"^[A-Za-z0-9_.\-]{1,64}$"

#: What the ticket reason names as the trigger.
PLANNER_TRIGGER = "manual"


def _allow(who: Who, permission: Permission) -> None:
    if isinstance(who, Principal):
        require(who, permission)
    elif who.role not in POLICY[permission].roles:
        raise PermissionDenied(f"{permission.value} is not allowed for this user")


def _scope(who: Who) -> Scope:
    return who.scope if isinstance(who, Principal) else who


# ---- models -----------------------------------------------------------------------


class AlgoInfo(BaseModel):
    name: str
    title: str
    description: str
    sliceable: bool = Field(description="Stonks can send it as child orders at other brokers.")
    params_schema: dict[str, Any]
    defaults: dict[str, Any]
    cost_assumption: dict[str, float] = Field(
        description="spread_factor, impact_factor and timing_bps the backtest assumes."
    )


class AlgoList(BaseModel):
    items: list[AlgoInfo]


class AlgoSettingView(BaseModel):
    portfolio_id: str
    strategy_id: str | None = Field(description="Empty: the portfolio's own setting.")
    algo: str
    params: dict[str, Any]
    updated_by: str | None
    updated_at: datetime


class AlgoSettingList(BaseModel):
    items: list[AlgoSettingView]


class AlgoSettingUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    algo: str = Field(min_length=1, max_length=32)
    params: dict[str, Any] = Field(default_factory=dict)
    strategy_id: Annotated[str, Field(pattern=_ID)] | None = None


class SliceView(BaseModel):
    seq: int
    client_id: str
    quantity: float
    send_after: datetime
    status: Literal["planned", "sent", "skipped"]
    status_reason: str | None
    order_state: str | None
    filled: float


class ParentOrderView(BaseModel):
    """A parent order Stonks works as child slices."""

    client_id: str
    portfolio_id: str
    ticker: str
    side: Literal["buy", "sell"]
    quantity: float
    filled: float
    algo: str
    state: str
    window_start: datetime
    window_end: datetime
    slices: list[SliceView]


class ParentOrderList(BaseModel):
    items: list[ParentOrderView]


class PlanTarget(BaseModel):
    model_config = ConfigDict(extra="forbid")

    ticker: str = Field(pattern=_TICKER)
    weight: float = Field(ge=0.0, le=1.0)


class PlanRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    portfolio_id: str = Field(pattern=_ID)
    source: Literal["strategy", "targets"] = Field(
        description="strategy: the weights of the strategy's test book; targets: your list."
    )
    strategy_id: Annotated[str, Field(pattern=_ID)] | None = None
    targets: list[PlanTarget] = Field(default_factory=list, max_length=500)
    min_trade_value: float = Field(default=0.0, ge=0.0)
    short_term_rate: float | None = Field(default=None, ge=0.0, le=1.0)
    long_term_rate: float | None = Field(default=None, ge=0.0, le=1.0)


class PlanConfirm(PlanRequest):
    reason: str = Field(min_length=3, max_length=500)
    key: Annotated[str, Field(pattern=_ID)] | None = Field(
        default=None, description="Idempotency key. Default: one from the plan's trades."
    )


class LotSaleView(BaseModel):
    acquired: date
    quantity: float
    cost_basis: float
    proceeds: float
    gain: float
    holding_period: Literal["short", "long"]


class PlanTaxView(BaseModel):
    lots: list[LotSaleView]
    gain: float
    short_term_gain: float
    long_term_gain: float
    estimated_tax: float | None


class PlanLineView(BaseModel):
    ticker: str
    price: float | None
    current_quantity: float
    current_weight: float
    target_weight: float
    target_quantity: float
    side: Literal["buy", "sell"] | None
    quantity: float
    value: float
    cost_bps: float | None
    cost: float
    weight_after: float
    skipped: str | None
    tax: PlanTaxView | None


class RebalancePlanView(BaseModel):
    portfolio_id: str
    source: Literal["strategy", "targets"]
    strategy_id: str | None
    as_of: date
    equity: float
    cash_before: float
    cash_after: float
    turnover: float = Field(description="Traded value over the book's value.")
    total_cost: float
    buys: float
    sells: float
    cash_weight_after: float
    max_drift_after: float
    tax_total: float | None
    algo: dict[str, Any] | None = Field(description="The execution algo the trades would use.")
    lines: list[PlanLineView]
    notes: list[str]


class PlanConfirmResult(BaseModel):
    plan: RebalancePlanView
    key: str
    ticket_ids: list[str]
    written: int = Field(description="New tickets. A repeat of the same key writes none.")


# ---- the service ------------------------------------------------------------------


class ExecutionService:
    def __init__(self, context: AppContext, *, clock: Callable[[], datetime] | None = None) -> None:
        self._ctx = context
        self._clock = clock or (lambda: datetime.now(UTC))

    # ---- algos ----------------------------------------------------------------------

    def algos(self, who: Who) -> AlgoList:
        _allow(who, Permission.READ)
        return AlgoList(items=[AlgoInfo(**get_algo(n).describe()) for n in algo_names()])

    def settings(self, who: Who, portfolio_id: str) -> AlgoSettingList:
        _allow(who, Permission.READ)
        with self._ctx.state() as state:
            self._portfolio(state, who, portfolio_id)
            return AlgoSettingList(
                items=[_setting_view(s) for s in list_settings(state, portfolio_id)]
            )

    def set_algo(self, who: Who, portfolio_id: str, body: AlgoSettingUpdate) -> AlgoSettingView:
        """Work the portfolio's orders (or one strategy's) with an algo."""
        _allow(who, Permission.PORTFOLIO_MANAGE)
        with self._ctx.state() as state:
            self._portfolio(state, who, portfolio_id)
            try:
                with state.transaction():
                    row = set_setting(
                        state,
                        portfolio_id,
                        body.algo,
                        body.params,
                        strategy_id=body.strategy_id,
                        actor=_scope(who).actor,
                        now=self._clock(),
                    )
                    AuditLog(state).record(
                        _scope(who).actor,
                        "execution_algo.set",
                        "portfolio",
                        portfolio_id,
                        portfolio_id=portfolio_id,
                        details={"algo": row.algo, "params": dict(row.params),
                                 "strategy_id": body.strategy_id},
                    )  # fmt: skip
            except AlgoParamsError as exc:
                raise ValidationError(str(exc)) from None
            return _setting_view(row)

    def clear_algo(self, who: Who, portfolio_id: str, strategy_id: str | None = None) -> bool:
        """Back to plain orders (or to the portfolio's setting)."""
        _allow(who, Permission.PORTFOLIO_MANAGE)
        with self._ctx.state() as state:
            self._portfolio(state, who, portfolio_id)
            with state.transaction():
                removed = clear_setting(state, portfolio_id, strategy_id)
                if removed:
                    AuditLog(state).record(
                        _scope(who).actor,
                        "execution_algo.clear",
                        "portfolio",
                        portfolio_id,
                        portfolio_id=portfolio_id,
                        details={"strategy_id": strategy_id},
                    )
            return removed

    def parents(self, who: Who, portfolio_id: str) -> ParentOrderList:
        """The parent orders Stonks works as child slices, newest first."""
        _allow(who, Permission.READ)
        with self._ctx.state() as state:
            self._portfolio(state, who, portfolio_id)
            return ParentOrderList(
                items=[_parent_view(p) for p in list_parents(state, [portfolio_id])]
            )

    # ---- the planner ------------------------------------------------------------------

    def plan(self, who: Who, body: PlanRequest) -> RebalancePlanView:
        """The trades that reach the targets. Nothing is written or sent."""
        _allow(who, Permission.READ)
        return self._plan(who, body)[0]

    def confirm(self, who: Who, body: PlanConfirm) -> PlanConfirmResult:
        """One order ticket per trade of the plan, each waiting for approval."""
        _allow(who, Permission.PORTFOLIO_TRADE)
        from stonks.production.tickets import list_tickets, submit_window, write_tickets

        view, plan, account = self._plan(who, body)
        trades = plan.trades
        if not trades:
            raise ValidationError("the plan has no trades to confirm")
        key = body.key or _plan_key(account.id, plan)
        now = self._clock()
        orders = [
            Order(
                client_id=f"plan:{account.id}:{key}:{line.ticker}:{line.side}",
                ticker=line.ticker,
                side=line.side,  # type: ignore[arg-type]
                quantity=line.quantity,
                order_type="market",
                portfolio_id=account.id,
                decision_price=line.price,
                decided_at=now,
                decision_context={
                    "trigger": PLANNER_TRIGGER,
                    "source": "planner",
                    "plan_key": key,
                    "planned_from": body.strategy_id if body.source == "strategy" else "targets",
                    "target_weight": line.target_weight,
                    "note": body.reason,
                },
                expected_cost_bps=line.cost_bps,
                position_effect="close" if line.side == "sell" else "open",
            )
            for line in trades
        ]
        with self._ctx.state() as state:
            window = submit_window(plan.as_of, self._ctx.settings.production.live.submit)
            written = write_tickets(
                state,
                orders,
                portfolio_id=account.id,
                tick_id=None,
                as_of=plan.as_of,
                window=window,
                hold=lambda _o: "approve_mode",
                now=now,
            )
            AuditLog(state).record(
                _scope(who).actor,
                "planner.confirm",
                "portfolio",
                account.id,
                portfolio_id=account.id,
                details={"key": key, "trades": len(orders), "written": len(written),
                         "reason": body.reason},
            )  # fmt: skip
            ids = {o.client_id for o in orders}
            tickets = [
                t for t in list_tickets(state, portfolio_ids=[account.id]) if t.client_id in ids
            ]
        return PlanConfirmResult(
            plan=view, key=key, ticket_ids=sorted(t.id for t in tickets), written=len(written)
        )

    def _plan(
        self, who: Who, body: PlanRequest
    ) -> tuple[RebalancePlanView, RebalancePlan, AccountPortfolio]:
        from stonks.production.tick import _load_or_seed_portfolio

        today = self._clock().astimezone(UTC).date()
        with self._ctx.state() as state:
            account = self._portfolio(state, who, body.portfolio_id)
            targets = self._targets(state, body)
            initial = (
                float(account.initial_cash)
                if account.initial_cash is not None
                else float(self._ctx.settings.production.initial_cash)
            )
            book = _load_or_seed_portfolio(state, initial, account.id)
            algo = resolve_algo(state, account.id, body.strategy_id)
        tickers = sorted(set(targets) | set(book.positions))
        prices, classes = self._market(tickers, today)
        lots = None
        if any(q > 0 for q in book.positions.values()):
            from stonks.app.tax import TaxService

            try:
                opened = TaxService(self._ctx).open_lots(account.id, today)
            except ValidationError:
                opened = None  # a missing FX rate leaves the preview out
            if opened is not None:
                lots = {}
                for lot in opened:
                    lots.setdefault(lot.ticker, []).append(lot)
        rates = None
        if body.short_term_rate is not None or body.long_term_rate is not None:
            rates = {"short": body.short_term_rate or 0.0, "long": body.long_term_rate or 0.0}
        costs = self._ctx.settings.backtest.costs
        if algo is not None:
            costs = costs.model_copy(update={"exec_algo": ExecAlgoAssumption(**algo)})
        try:
            plan = plan_rebalance(
                as_of=today,
                cash=book.cash,
                positions=book.positions,
                prices=prices,
                targets=targets,
                cost_model=costs.build(),
                asset_classes=classes,  # type: ignore[arg-type]
                min_trade_value=body.min_trade_value,
                lots=lots,
                tax_rates=rates,  # type: ignore[arg-type]
            )
        except PlanError as exc:
            raise ValidationError(str(exc)) from None
        return _plan_view(plan, body, algo), plan, account

    def _targets(self, state: SqliteState, body: PlanRequest) -> dict[str, float]:
        if body.source == "targets":
            if body.strategy_id is not None:
                raise ValidationError("a target list takes no strategy_id")
            out: dict[str, float] = {}
            for t in body.targets:
                if t.ticker in out:
                    raise ValidationError(f"{t.ticker} is listed twice")
                out[t.ticker] = t.weight
            return out
        if body.strategy_id is None:
            raise ValidationError("name the strategy (strategy_id)")
        if body.targets:
            raise ValidationError("a strategy plan takes no target list")
        if not state.sql("SELECT 1 FROM strategies WHERE id = ?", [body.strategy_id]):
            raise NotFoundError(f"strategy {body.strategy_id!r} not found")
        rows = state.sql(
            "SELECT ticker, model_weight FROM signals WHERE strategy_id = ? AND as_of ="
            " (SELECT MAX(as_of) FROM signals WHERE strategy_id = ? AND model_weight IS NOT NULL)"
            " AND model_weight IS NOT NULL",
            [body.strategy_id, body.strategy_id],
        )
        if not rows:
            raise ValidationError(f"strategy {body.strategy_id} has no test book weights yet")
        return {r["ticker"]: max(0.0, float(r["model_weight"])) for r in rows}

    def _market(self, tickers: list[str], day: date) -> tuple[dict[str, float], dict[str, str]]:
        if not tickers:
            return {}, {}
        with self._ctx.lake() as lake:
            df = lake.sql(
                "SELECT ticker, arg_max(close, date) AS close FROM prices"
                " WHERE ticker = ANY(?) AND date <= ? AND close IS NOT NULL GROUP BY ticker",
                [tickers, day],
            )
            classes = lake.get_asset_classes(tickers)
        prices = {str(r["ticker"]): float(r["close"]) for r in df.to_dict("records")}
        return prices, dict(classes)

    @staticmethod
    def _portfolio(state: SqliteState, who: Who, portfolio_id: str) -> AccountPortfolio:
        try:
            return owned_portfolio(state, _scope(who), portfolio_id)
        except NotFound as exc:
            raise NotFoundError(str(exc)) from None


def _plan_key(portfolio_id: str, plan: RebalancePlan) -> str:
    """A key from the plan's day and trades, so confirming the same plan
    twice writes its tickets once."""
    content = json.dumps(
        [portfolio_id, plan.as_of.isoformat(),
         [(t.ticker, t.side, t.quantity) for t in plan.trades]],
    )  # fmt: skip
    return hashlib.sha256(content.encode("utf-8")).hexdigest()[:12]


def _setting_view(s: AlgoSetting) -> AlgoSettingView:
    return AlgoSettingView(
        portfolio_id=s.portfolio_id,
        strategy_id=s.strategy_id,
        algo=s.algo,
        params=dict(s.params),
        updated_by=s.updated_by,
        updated_at=s.updated_at,
    )


def _parent_view(p: ParentView) -> ParentOrderView:
    return ParentOrderView(
        client_id=p.client_id,
        portfolio_id=p.portfolio_id,
        ticker=p.ticker,
        side=p.side,  # type: ignore[arg-type]
        quantity=p.quantity,
        filled=p.filled,
        algo=p.algo,
        state=p.state,
        window_start=p.window_start,
        window_end=p.window_end,
        slices=[SliceView(**s) for s in p.slices],
    )


def _plan_view(
    plan: RebalancePlan, body: PlanRequest, algo: dict[str, Any] | None
) -> RebalancePlanView:
    lines = []
    for line in plan.lines:
        tax = None
        if line.tax is not None:
            tax = PlanTaxView(
                lots=[LotSaleView(**s.__dict__) for s in line.tax.lots],
                gain=line.tax.gain,
                short_term_gain=line.tax.short_term_gain,
                long_term_gain=line.tax.long_term_gain,
                estimated_tax=line.tax.estimated_tax,
            )
        data = {k: v for k, v in line.__dict__.items() if k != "tax"}
        lines.append(PlanLineView(**data, tax=tax))
    return RebalancePlanView(
        portfolio_id=body.portfolio_id,
        source=body.source,
        strategy_id=body.strategy_id,
        as_of=plan.as_of,
        equity=plan.equity,
        cash_before=plan.cash_before,
        cash_after=plan.cash_after,
        turnover=plan.turnover,
        total_cost=plan.total_cost,
        buys=plan.buys,
        sells=plan.sells,
        cash_weight_after=plan.cash_weight_after,
        max_drift_after=plan.max_drift_after,
        tax_total=plan.tax_total,
        algo=algo,
        lines=lines,
        notes=list(plan.notes),
    )

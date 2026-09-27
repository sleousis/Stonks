"""ManualOrdersService: place, preview, change and cancel your own orders
on your portfolios (roadmap 20.1) for the API, MCP and the CLI.

The checks and the execution live in :mod:`stonks.production.manual`. This
service decides who may act and on which book:

- a principal needs ``portfolio.trade`` and must own the portfolio (404
  otherwise, admins included);
- a book that trades real money also needs ``orders.live``, a fresh second
  factor, so an API token (MCP) or the assistant can never place one;
- the CLI acts as a bare scope (``service:cli`` or ``--user``). A real-money
  order from the shell needs ``confirm_live`` (the typed confirmation).

The book's risk policy is the tick's: the global policy tightened by the
owner's limits and the portfolio's own. Its broker is the one the tick
would use: the linked connection's trader for a broker portfolio, the
configured broker for the default portfolio at an external broker, else
simulated fills on the Stonks ledger.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from stonks.accounts import NotFound, Role, Scope, owned_portfolio
from stonks.accounts import Portfolio as AccountPortfolio
from stonks.accounts.book import tighter_of
from stonks.accounts.models import DEFAULT_PORTFOLIO_ID
from stonks.app.context import AppContext
from stonks.app.errors import ConflictError, NotFoundError, ValidationError
from stonks.app.tick_summary import RiskAdjustmentView
from stonks.auth.errors import PermissionDenied
from stonks.auth.policy import Permission, require
from stonks.auth.principal import Principal
from stonks.core.protocols import Broker
from stonks.logging import get_logger
from stonks.production.manual import (
    CancelResult,
    ManualBook,
    ManualOrder,
    ManualOrderNotFound,
    ManualOrderRefused,
    ManualResult,
    cancel_order,
    change_manual_order,
    place_manual_order,
)
from stonks.production.settings_builder import build_tick_settings
from stonks.store.state import SqliteState

_log = get_logger("stonks.app.manual_orders")

#: What a person types in the CLI to place a real-money order.
LIVE_PHRASE = "PLACE LIVE ORDER"

#: The caller: a :class:`Principal` (API, MCP, assistant) or a bare
#: :class:`Scope` (the CLI).
Who = Scope | Principal

#: Opens the broker of a portfolio that trades at one.
BrokerOpener = Callable[[AccountPortfolio], Broker]

_TICKER = r"^[A-Za-z0-9][A-Za-z0-9._\-^=]{0,31}$"
_KEY = r"^[A-Za-z0-9_.\-]{1,64}$"


class ManualOrderRequest(BaseModel):
    """One order you place by hand. It goes through the kill switch, every
    halt and every risk rule of the book, like a strategy's order."""

    model_config = ConfigDict(extra="forbid")

    portfolio_id: str | None = Field(
        default=None, max_length=64, description="One of your portfolios. Default: your own book."
    )
    ticker: str = Field(pattern=_TICKER, description="Instrument id, e.g. AAPL.US")
    side: Literal["buy", "sell"]
    quantity: float = Field(gt=0, le=1e9)
    order_type: Literal["market", "limit"] = "market"
    limit_price: float | None = Field(default=None, gt=0, description="Needed for a limit order.")
    reason: str = Field(min_length=3, max_length=500, description="Why you trade (recorded).")
    client_id: str | None = Field(
        default=None,
        pattern=_KEY,
        description="Your idempotency key. The same key places the order once.",
    )
    allow_reduce: bool = Field(
        default=False,
        description="Place a smaller order when a risk rule shrinks it. Off: the order is "
        "refused and the answer says what the rules allow.",
    )


class ManualOrderChange(BaseModel):
    """A new quantity or limit for one of your working manual orders. It is
    cancelled at the broker and replaced by a new order."""

    model_config = ConfigDict(extra="forbid")

    portfolio_id: str | None = Field(default=None, max_length=64)
    quantity: float | None = Field(default=None, gt=0, le=1e9)
    limit_price: float | None = Field(default=None, gt=0)
    reason: str = Field(min_length=3, max_length=500)
    allow_reduce: bool = False


class OrderCancelRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    portfolio_id: str | None = Field(default=None, max_length=64)
    reason: str = Field(min_length=3, max_length=500)


class ManualOrderResult(BaseModel):
    client_id: str
    portfolio_id: str
    ticker: str
    side: Literal["buy", "sell"]
    order_type: Literal["market", "limit"]
    limit_price: float | None
    status: Literal["preview", "pending", "filled", "partially_filled", "rejected", "cancelled"]
    requested_quantity: float
    quantity: float = Field(description="What was placed (smaller when allow_reduce applied).")
    reference_price: float = Field(description="The latest close the order was checked at.")
    fill_price: float | None = None
    reason: str | None = Field(default=None, description="Why it ended in its status.")
    adjustments: list[RiskAdjustmentView] = Field(default_factory=list)
    duplicate: bool = Field(default=False, description="The client id was placed before.")
    halt: str | None = Field(default=None, description="A halt that limits the book.")
    live: bool = Field(description="The book trades real money.")


class OrderCancelResult(BaseModel):
    client_id: str
    status: str
    cancelled: bool


def _scope(who: Who) -> Scope:
    return who.scope if isinstance(who, Principal) else who


class ManualOrdersService:
    def __init__(
        self,
        context: AppContext,
        *,
        brokers: BrokerOpener | None = None,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._ctx = context
        self._brokers = brokers
        self._clock = clock or (lambda: datetime.now(UTC))

    # ---- entry points --------------------------------------------------------------

    def place(
        self, who: Who, body: ManualOrderRequest, *, confirm_live: bool = False
    ) -> ManualOrderResult:
        return self._place(who, body, preview=False, confirm_live=confirm_live)

    def preview(self, who: Who, body: ManualOrderRequest) -> ManualOrderResult:
        """Every check, nothing placed. Needs no second factor, even for a
        real-money book, because it cannot trade."""
        return self._place(who, body, preview=True, confirm_live=True)

    def change(
        self, who: Who, client_id: str, body: ManualOrderChange, *, confirm_live: bool = False
    ) -> ManualOrderResult:
        if body.quantity is None and body.limit_price is None:
            raise ValidationError("give a new quantity or a new limit price")
        with self._ctx.state() as state:
            account = self._portfolio(state, who, body.portfolio_id)
            live = self._live(account)
            self._authorize(who, live, confirm_live)
            book = self._book(state, account)
            tick = build_tick_settings(self._ctx.settings, [])
            with self._ctx.lake() as lake:
                try:
                    result = change_manual_order(
                        state,
                        lake,
                        client_id,
                        book,
                        tick,
                        actor=_scope(who).actor,
                        reason=body.reason,
                        quantity=body.quantity,
                        limit_price=body.limit_price,
                        allow_reduce=body.allow_reduce,
                        now=self._clock(),
                    )
                except ManualOrderNotFound as exc:
                    raise NotFoundError(str(exc)) from None
                except ManualOrderRefused as exc:
                    raise _conflict(exc) from None
            return self._view(state, result, account.id, live, _as_order(state, result))

    def cancel(
        self, who: Who, client_id: str, body: OrderCancelRequest, *, confirm_live: bool = False
    ) -> OrderCancelResult:
        """Cancel one working order of your portfolio (yours or a
        strategy's). Cancelling only lowers risk, so no second factor."""
        with self._ctx.state() as state:
            account = self._portfolio(state, who, body.portfolio_id)
            self._authorize(who, False, confirm_live)
            broker = self._broker(account)
            try:
                result: CancelResult = cancel_order(
                    state,
                    account.id,
                    client_id,
                    broker=broker,
                    actor=_scope(who).actor,
                    reason=body.reason,
                )
            except ManualOrderNotFound as exc:
                raise NotFoundError(str(exc)) from None
            except ManualOrderRefused as exc:
                raise _conflict(exc) from None
        return OrderCancelResult(
            client_id=result.client_id, status=result.status, cancelled=result.cancelled
        )

    # ---- internals -------------------------------------------------------------------

    def _place(
        self, who: Who, body: ManualOrderRequest, *, preview: bool, confirm_live: bool
    ) -> ManualOrderResult:
        if body.order_type == "limit" and body.limit_price is None:
            raise ValidationError("a limit order needs limit_price")
        if body.order_type == "market" and body.limit_price is not None:
            raise ValidationError("a market order takes no limit_price")
        with self._ctx.state() as state:
            account = self._portfolio(state, who, body.portfolio_id)
            live = self._live(account)
            self._authorize(who, live and not preview, confirm_live)
            book = self._book(state, account)
            tick = build_tick_settings(self._ctx.settings, [])
            order = ManualOrder(
                portfolio_id=account.id,
                ticker=body.ticker.upper(),
                side=body.side,
                quantity=body.quantity,
                reason=body.reason.strip(),
                actor=_scope(who).actor,
                order_type=body.order_type,
                limit_price=body.limit_price,
                client_key=body.client_id,
                allow_reduce=body.allow_reduce,
            )
            with self._ctx.lake() as lake:
                try:
                    result = place_manual_order(
                        state, lake, order, book, tick, preview=preview, now=self._clock()
                    )
                except ManualOrderRefused as exc:
                    raise _conflict(exc) from None
            return self._view(state, result, account.id, live, order)

    def _portfolio(
        self, state: SqliteState, who: Who, portfolio_id: str | None
    ) -> AccountPortfolio:
        scope = _scope(who)
        if isinstance(who, Principal):
            require(who, Permission.PORTFOLIO_TRADE)
        elif not (scope.is_service or Role(scope.role).can_trade):
            raise PermissionDenied("manual orders need a role that can trade")
        pid = portfolio_id or self._default_portfolio(state, scope)
        try:
            account = owned_portfolio(state, scope, pid)
        except NotFound as exc:
            raise NotFoundError(str(exc)) from None
        if account.status != "active":
            raise ConflictError(f"portfolio {account.id} is {account.status}")
        return account

    def _default_portfolio(self, state: SqliteState, scope: Scope) -> str:
        if scope.is_service:
            raise ValidationError("name the portfolio (portfolio_id)")
        rows = state.sql(
            "SELECT id FROM portfolios WHERE owner_id = ? AND status = 'active'"
            " ORDER BY id != ?, created_at, id LIMIT 1",
            [scope.user_id, DEFAULT_PORTFOLIO_ID],
        )
        if not rows:
            raise NotFoundError("you have no portfolio yet")
        return str(rows[0]["id"])

    def _authorize(self, who: Who, live: bool, confirm_live: bool) -> None:
        if not live:
            return
        if isinstance(who, Principal):
            require(who, Permission.ORDER_LIVE)
        elif not confirm_live:
            raise PermissionDenied(f"this book trades real money: confirm by typing {LIVE_PHRASE}")

    def _live(self, account: AccountPortfolio) -> bool:
        """Whether orders in ``account`` are real money (the same rule as
        the trading-mode view): a broker portfolio, or the default one at a
        live Alpaca account."""
        if account.kind == "broker":
            return True
        brokers = self._ctx.settings.brokers
        if account.id == DEFAULT_PORTFOLIO_ID and brokers.kind == "alpaca":
            return not brokers.alpaca.paper and brokers.alpaca.allow_live
        return False

    def _book(self, state: SqliteState, account: AccountPortfolio) -> ManualBook:
        row = state.sql(
            "SELECT p.paper_of, u.risk_policy_json FROM portfolios p"
            " JOIN users u ON u.id = p.owner_id WHERE p.id = ?",
            [account.id],
        )[0]
        settings = self._ctx.settings
        risk = tighter_of(
            settings.production.risk,
            json.loads(row["risk_policy_json"] or "{}") or None,
            account.risk_policy,
        )
        return ManualBook(
            portfolio_id=account.id,
            owner_id=account.owner_id,
            risk=risk,
            initial_cash=(
                float(account.initial_cash)
                if account.initial_cash is not None
                else float(settings.production.initial_cash)
            ),
            allow_short=account.allow_short,
            parent_id=row["paper_of"],
            broker=self._broker(account),
        )

    def _broker(self, account: AccountPortfolio) -> Broker | None:
        settings = self._ctx.settings
        external_default = (
            account.id == DEFAULT_PORTFOLIO_ID and settings.brokers.kind != "simulated"
        )
        if account.kind != "broker" and not external_default:
            return None
        if self._brokers is not None:
            return self._brokers(account)
        if account.kind == "broker":
            return _connection_trader(self._ctx, account)
        from stonks.core.types import Portfolio
        from stonks.execution.brokers import make_broker

        return make_broker(settings, Portfolio(cash=0.0))

    def _view(
        self,
        state: SqliteState,
        result: ManualResult,
        portfolio_id: str,
        live: bool,
        order: ManualOrder,
    ) -> ManualOrderResult:
        rows = state.sql(
            "SELECT ticker, side, order_type, limit_price FROM orders WHERE client_id = ?",
            [result.client_id],
        )
        row: dict[str, Any] = dict(rows[0]) if rows else {}
        return ManualOrderResult(
            client_id=result.client_id,
            portfolio_id=portfolio_id,
            ticker=row.get("ticker") or order.ticker,
            side=row.get("side") or order.side,
            order_type=row.get("order_type") or order.order_type,
            limit_price=row["limit_price"] if row else order.limit_price,
            status=result.status,
            requested_quantity=result.requested_quantity,
            quantity=result.quantity,
            reference_price=result.reference_price,
            fill_price=result.fill_price,
            reason=result.reason,
            adjustments=[RiskAdjustmentView(**a) for a in result.adjustments],
            duplicate=result.duplicate,
            halt=result.halt,
            live=live,
        )


def _connection_trader(context: AppContext, account: AccountPortfolio) -> Broker:
    """The linked connection's trading adapter, opened as the scheduler
    does for auto books (the owner check was done by the caller)."""
    from stonks.connections.service import ConnectionService
    from stonks.connections.settings import ConnectionsConfig

    with context.state() as state:
        service = ConnectionService(state, ConnectionsConfig.load())
        try:
            return service.open_trader(Scope.service("manual_orders"), account.id)
        except Exception as exc:
            raise ConflictError(
                f"the broker of portfolio {account.id} cannot trade: {exc}"
            ) from None


def _as_order(state: SqliteState, result: ManualResult) -> ManualOrder:
    """The recorded order behind ``result`` (a change's replacement)."""
    row = state.sql("SELECT * FROM orders WHERE client_id = ?", [result.client_id])[0]
    return ManualOrder(
        portfolio_id=row["portfolio_id"],
        ticker=row["ticker"],
        side=row["side"],
        quantity=float(row["quantity"]),
        reason=row["manual_reason"] or "",
        actor=row["placed_by"] or "",
        order_type=row["order_type"],
        limit_price=row["limit_price"],
    )


class OrderRefusedError(ConflictError):
    """A manual order failed a check (409 ``order_refused``). The problem
    body lists what the risk rules would have done (``risk_adjustments``)."""

    code = "order_refused"

    def __init__(self, message: str, adjustments: tuple[dict[str, Any], ...] = ()) -> None:
        super().__init__(message)
        self.adjustments = [dict(a) for a in adjustments]

    def problem_extensions(self) -> dict[str, Any]:
        return {"risk_adjustments": self.adjustments} if self.adjustments else {}


def _conflict(exc: ManualOrderRefused) -> OrderRefusedError:
    return OrderRefusedError(str(exc), exc.adjustments)

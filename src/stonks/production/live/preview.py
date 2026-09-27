"""The live dry-run preview (roadmap 19.9, design section 4).

``stonks live preview <portfolio>`` and the console's "Preview the next
orders" button:

1. run the tick's decision for the portfolio's live book on today's data,
   as a dry run (only a dry-run tick row is written);
2. read the live account (positions, cash, buying power) from the broker;
3. run the risk rules, live safeguards and account rules against it;
4. ask the broker's what-if (``MarginPreviewer``) for every order;
5. return the orders it would send, with what each rule changed and why.

It **never transmits**. The broker is wrapped in :class:`PreviewBroker`,
which refuses every call that places or cancels an order, whatever the
tick does. The preview needs trade permission but no second factor.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass, field, replace
from datetime import date
from typing import Any

from stonks.core.protocols import Broker
from stonks.core.types import Fill, Order, Portfolio
from stonks.execution.brokers.base import (
    AccountReader,
    BrokerOrderState,
    LiveAccountState,
    MarginPreview,
    MarginPreviewer,
    OrderStateSource,
    Quote,
    QuoteSource,
)
from stonks.logging import get_logger
from stonks.production.live.stages import LiveStage, get_stage
from stonks.production.rules import RiskAdjustment
from stonks.store.state import SqliteState

_log = get_logger("stonks.production.live.preview")


class PreviewError(ValueError):
    """The portfolio has no live book to preview."""


class TransmitRefusedError(RuntimeError):
    """A preview tried to place or cancel an order. It never may."""


class PreviewBroker:
    """A read-only view of a broker: reads, quotes and what-if pass through,
    and every call that would send or cancel an order raises."""

    def __init__(self, inner: Broker) -> None:
        self.inner = inner

    # reads
    def fetch_portfolio(self) -> Portfolio:
        return self.inner.fetch_portfolio()

    def get_order_state(self, client_id: str) -> BrokerOrderState | None:
        if isinstance(self.inner, OrderStateSource):
            return self.inner.get_order_state(client_id)
        return None

    def fetch_account(self) -> LiveAccountState:
        if not isinstance(self.inner, AccountReader):
            raise TypeError("the broker cannot read the account")
        return self.inner.fetch_account()

    def quotes(self, tickers: Sequence[str]) -> dict[str, Quote]:
        if not isinstance(self.inner, QuoteSource):
            return {}
        return dict(self.inner.quotes(tickers))

    def what_if(self, order: Order) -> MarginPreview:
        if not isinstance(self.inner, MarginPreviewer):
            raise TypeError("the broker has no what-if preview")
        return self.inner.what_if(order)

    def reconcile(self) -> list[Fill]:
        return []

    # writes: never
    def place_order(self, order: Order) -> Fill | None:
        raise TransmitRefusedError(f"a preview never sends orders ({order.client_id})")

    def cancel_order(self, client_id: str) -> bool:
        raise TransmitRefusedError(f"a preview never cancels orders ({client_id})")

    def cancel_all(self) -> int:
        raise TransmitRefusedError("a preview never cancels orders")

    def close(self) -> None:
        close = getattr(self.inner, "close", None)
        if callable(close):
            close()


@dataclass(frozen=True)
class PreviewOrder:
    client_id: str
    ticker: str
    side: str
    quantity: float
    order_type: str
    limit_price: float | None
    position_effect: str | None
    strategy_id: str | None
    #: Quantity times the limit, else the decision price.
    notional: float | None
    time_in_force: str | None = None
    what_if: MarginPreview | None = None
    #: Why the what-if failed (a failed what-if never lets a buy through).
    what_if_error: str | None = None
    #: What the risk rules, safeguards and account rules did to this ticker.
    adjustments: tuple[RiskAdjustment, ...] = ()


@dataclass(frozen=True)
class LivePreview:
    portfolio_id: str
    as_of: date
    stage: LiveStage
    #: The book's outcome in the dry run (``ok``, ``noop``, ``error``, ...).
    status: str
    #: Why the book decided nothing, when it did.
    reason: str | None
    orders: tuple[PreviewOrder, ...] = ()
    #: Every adjustment, dropped orders included.
    adjustments: tuple[RiskAdjustment, ...] = ()
    account: LiveAccountState | None = None
    allocation: float | None = None
    #: Whether the broker answers what-if requests.
    what_if_available: bool = False
    tick_id: str | None = None
    #: Always false: a preview never transmits.
    transmitted: bool = False
    notes: tuple[str, ...] = field(default_factory=tuple[str, ...])


@dataclass
class _Capture:
    orders: list[PreviewOrder] = field(default_factory=list[PreviewOrder])
    adjustments: list[RiskAdjustment] = field(default_factory=list[RiskAdjustment])
    account: LiveAccountState | None = None
    allocation: float | None = None
    what_if: bool = False
    seen: bool = False


def run_preview(
    state: SqliteState,
    lake: Any,
    registry: Any,
    settings: Any,
    plan: Any,
    portfolio_id: str,
    *,
    broker_factory: Any = None,
    as_of: date | None = None,
) -> LivePreview:
    """Preview ``portfolio_id``'s live book with a dry-run tick over
    ``plan`` (``load_tick_plan(..., dry_run=True)``). Every broker the tick
    opens is wrapped in :class:`PreviewBroker` and closed afterwards."""
    from stonks.production.tick import BookDecision, run_tick

    books = [b for b in plan.books if b.portfolio_id == portfolio_id and b.mode != "paper"]
    if not books:
        raise PreviewError(
            f"portfolio {portfolio_id!r} has no live book: it needs an approve or auto"
            " subscription at a broker"
        )
    opened: list[PreviewBroker] = []

    def wrap(open_broker: Callable[[Any], Broker]) -> Callable[[Any], Broker]:
        def opener(arg: Any) -> Broker:
            wrapped = PreviewBroker(open_broker(arg))
            opened.append(wrapped)
            return wrapped

        return opener

    traders = plan.traders
    preview_plan = replace(
        plan,
        books=tuple(books),
        notify=(),
        traders=wrap(traders) if traders is not None else None,
        notices=(),
    )
    factory = wrap(broker_factory) if broker_factory is not None else None
    capture = _Capture()

    def sink(decision: BookDecision) -> None:
        capture.seen = True
        capture.adjustments.extend(decision.adjustments)
        live = decision.live
        if live is not None:
            capture.account = live.account
            capture.allocation = live.allocation
        broker = decision.broker
        capture.what_if = isinstance(broker, PreviewBroker) and isinstance(
            broker.inner, MarginPreviewer
        )
        for order in decision.orders:
            capture.orders.append(_preview_order(order, broker, decision.adjustments))

    try:
        result = run_tick(
            state,
            lake,
            registry,
            settings,
            as_of=as_of,
            dry_run=True,
            broker_factory=factory,
            plan=preview_plan,
            order_sink=sink,
        )
    finally:
        for broker in opened:
            try:
                broker.close()
            except Exception as exc:  # pragma: no cover - best effort
                _log.warning("live.preview_close_failed", error=str(exc))
    book = next((r for r in result.portfolios if r.portfolio_id == portfolio_id), None)
    status = book.status if book is not None else result.status
    summary = book.summary if book is not None else {}
    notes: list[str] = []
    if capture.seen and not capture.what_if:
        notes.append("the broker has no what-if preview: margin and commission are not shown")
    return LivePreview(
        portfolio_id=portfolio_id,
        as_of=_as_of(result.tick_id),
        stage=get_stage(state, portfolio_id),
        status=status,
        reason=summary.get("reason") or summary.get("error"),
        orders=tuple(capture.orders),
        adjustments=tuple(capture.adjustments),
        account=capture.account,
        allocation=capture.allocation,
        what_if_available=capture.what_if,
        tick_id=result.tick_id,
        notes=tuple(notes),
    )


def _preview_order(
    order: Order, broker: Broker | None, adjustments: Sequence[RiskAdjustment]
) -> PreviewOrder:
    price = order.limit_price if order.limit_price is not None else order.decision_price
    what_if: MarginPreview | None = None
    error: str | None = None
    if isinstance(broker, PreviewBroker) and isinstance(broker.inner, MarginPreviewer):
        try:
            what_if = broker.what_if(order)
        except Exception as exc:
            error = str(exc) or type(exc).__name__
    return PreviewOrder(
        client_id=order.client_id or "",
        ticker=order.ticker,
        side=order.side,
        quantity=order.quantity,
        order_type=order.order_type,
        limit_price=order.limit_price,
        position_effect=order.position_effect,
        strategy_id=order.strategy_id,
        notional=order.quantity * price if price is not None else None,
        time_in_force=order.time_in_force,
        what_if=what_if,
        what_if_error=error,
        adjustments=tuple(a for a in adjustments if a.ticker == order.ticker),
    )


def _as_of(tick_id: str) -> date:
    """The dry run's date, from its tick id (``tick_<date>_<hex>``)."""
    return date.fromisoformat(tick_id.split("_")[1])

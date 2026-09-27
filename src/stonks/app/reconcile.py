"""ReconcileService: the reconciliation reports of a person's live
portfolios (roadmap 19.5).

Each report is one check of a portfolio against its broker (start of day,
submit, end of day or by hand): its status, the unexplained drift items,
what the check explained itself, the owner's own positions and orders it
kept apart, the ``broker_drift`` halt it opened and the auto subscriptions
it paused.

Reading needs ``data.read`` and the portfolio. Another person's
portfolio and its reports read as missing, admins included.
"""

from __future__ import annotations

from datetime import date
from typing import Any, Literal

from pydantic import BaseModel

from stonks.accounts import NotFound, PortfolioRepository, owned_portfolio
from stonks.app.context import AppContext
from stonks.app.errors import NotFoundError
from stonks.auth.policy import Permission, require
from stonks.auth.principal import Principal
from stonks.execution.drift import DriftItem
from stonks.production.live.checks import ReconcileReport, get_report, list_reports


class DriftItemView(BaseModel):
    #: ``position_qty``, ``unknown_position``, ``unknown_order``,
    #: ``missing_order``, ``order_state``, ``unknown_execution`` (material),
    #: or ``unresolved_order``, ``stuck_order``, ``commission_missing``,
    #: ``stale_order`` (alert only).
    kind: str
    #: The ticker, our client id or the broker's order id.
    key: str
    ours: float | str | None
    broker: float | str | None
    #: Material items open the ``broker_drift`` halt.
    material: bool
    explained: bool
    detail: str


class ExternalOrderView(BaseModel):
    broker_order_id: str
    ticker: str
    side: str
    quantity: float


class ExternalView(BaseModel):
    """The owner's own holdings and hand-placed orders: never drift."""

    positions: dict[str, float]
    orders: list[ExternalOrderView]


class ReconcileReportView(BaseModel):
    id: str
    portfolio_id: str
    kind: Literal["sod", "submit", "eod", "adhoc"]
    as_of: date
    taken_at: str
    status: Literal["clean", "warn", "drift", "outage", "fault"]
    items: list[DriftItemView]
    explained: list[DriftItemView]
    external: ExternalView
    #: What reconciliation did first (fills booked, orders updated).
    summary: dict[str, Any]
    #: Why the broker could not be read (outage and fault only).
    detail: str | None
    #: The ``broker_drift`` halt this check opened or found open.
    halt_id: int | None
    #: The auto subscriptions this check paused.
    paused: list[str]


class ReconcileService:
    def __init__(self, context: AppContext) -> None:
        self._ctx = context

    def reports(
        self, principal: Principal, *, portfolio_id: str | None = None, limit: int = 50
    ) -> list[ReconcileReportView]:
        """Newest first, of one portfolio or of all your own."""
        require(principal, Permission.READ)
        with self._ctx.state() as state:
            if portfolio_id is not None:
                try:
                    ids = [owned_portfolio(state, principal.scope, portfolio_id).id]
                except NotFound as exc:
                    raise NotFoundError(str(exc)) from exc
            else:
                ids = [p.id for p in PortfolioRepository(state).list(principal.scope)]
            found = list_reports(state, portfolio_ids=ids, limit=limit)
        return [report_view(r) for r in found]

    def report(self, principal: Principal, report_id: str) -> ReconcileReportView:
        require(principal, Permission.READ)
        with self._ctx.state() as state:
            found = get_report(state, report_id)
            try:
                if found is None:
                    raise NotFound(f"reconcile report {report_id!r} not found")
                owned_portfolio(state, principal.scope, found.portfolio_id)
            except NotFound as exc:
                raise NotFoundError(f"reconcile report {report_id!r} not found") from exc
        return report_view(found)


def _item(i: DriftItem) -> DriftItemView:
    return DriftItemView(
        kind=i.kind,
        key=i.key,
        ours=i.ours,
        broker=i.broker,
        material=i.material,
        explained=i.explained,
        detail=i.detail,
    )


def report_view(r: ReconcileReport) -> ReconcileReportView:
    external = dict(r.external)
    return ReconcileReportView(
        id=r.id,
        portfolio_id=r.portfolio_id,
        kind=r.kind,
        as_of=r.as_of,
        taken_at=r.taken_at,
        status=r.status,
        items=[_item(i) for i in r.items],
        explained=[_item(i) for i in r.explained],
        external=ExternalView(
            positions={str(k): float(v) for k, v in (external.get("positions") or {}).items()},
            orders=[ExternalOrderView(**o) for o in external.get("orders") or []],
        ),
        summary=dict(r.summary),
        detail=r.detail,
        halt_id=r.halt_id,
        paused=list(r.paused),
    )

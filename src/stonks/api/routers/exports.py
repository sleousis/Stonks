"""CSV downloads (roadmap 13.12). Each route answers ``text/csv`` as an
attachment. Portfolio files are about one of your portfolios (404
otherwise); lab trials need ``lab.run``."""

from __future__ import annotations

from datetime import date
from typing import Annotated, Any

from fastapi import APIRouter, Query, Response

from stonks.api.deps import PortfolioIdDep, PrincipalDep, ServicesDep, needs
from stonks.api.errors import PROBLEM_RESPONSES
from stonks.app.exports import ExportKind, ExportService, filename
from stonks.auth import Permission

router = APIRouter(prefix="/api/exports", tags=["exports"], responses=PROBLEM_RESPONSES)

_CSV: dict[int | str, dict[str, Any]] = {
    200: {
        "description": "A CSV file with a header row",
        "content": {"text/csv": {"schema": {"type": "string", "format": "binary"}}},
    }
}


def _service(services: ServicesDep) -> ExportService:
    return ExportService(services.context, services.portfolio)


def _csv(body: str, kind: ExportKind, scope: str) -> Response:
    return Response(
        content=body.encode("utf-8"),
        media_type="text/csv; charset=utf-8",
        headers={
            "Content-Disposition": f'attachment; filename="{filename(kind, scope)}"',
            "Cache-Control": "no-store",
        },
    )


@router.get(
    "/orders",
    operation_id="exportOrders",
    response_class=Response,
    responses=_CSV,
    summary="Orders as CSV",
)
def export_orders(services: ServicesDep, portfolio_id: PortfolioIdDep) -> Response:
    """Every order of one of your portfolios, newest first."""
    return _csv(_service(services).orders(portfolio_id), "orders", portfolio_id)


@router.get(
    "/fills",
    operation_id="exportFills",
    response_class=Response,
    responses=_CSV,
    summary="Fills as CSV",
)
def export_fills(services: ServicesDep, portfolio_id: PortfolioIdDep) -> Response:
    """Every fill of one of your portfolios, newest first."""
    return _csv(_service(services).fills(portfolio_id), "fills", portfolio_id)


@router.get(
    "/journal",
    operation_id="exportJournal",
    response_class=Response,
    responses=_CSV,
    summary="Trade journal as CSV",
)
def export_journal(services: ServicesDep, portfolio_id: PortfolioIdDep) -> Response:
    """The trade journal: each order with its reason, costs and your notes."""
    return _csv(_service(services).journal(portfolio_id), "journal", portfolio_id)


@router.get(
    "/snapshots",
    operation_id="exportSnapshots",
    response_class=Response,
    responses=_CSV,
    summary="Portfolio snapshots as CSV",
)
def export_snapshots(services: ServicesDep, portfolio_id: PortfolioIdDep) -> Response:
    """Every stored snapshot: cash, total value and positions, newest first."""
    return _csv(_service(services).snapshots(portfolio_id), "snapshots", portfolio_id)


@router.get(
    "/pnl",
    operation_id="exportPnl",
    response_class=Response,
    responses=_CSV,
    summary="Daily P&L as CSV",
)
def export_pnl(
    services: ServicesDep, portfolio_id: PortfolioIdDep, since: date | None = None
) -> Response:
    """Daily value, change, return and drawdown, oldest first."""
    return _csv(_service(services).pnl(portfolio_id, since=since), "pnl", portfolio_id)


@router.get(
    "/lab-trials",
    operation_id="exportLabTrials",
    response_class=Response,
    responses=_CSV,
    summary="Lab trial results as CSV",
    dependencies=needs(Permission.LAB_RUN),
)
def export_lab_trials(
    services: ServicesDep,
    principal: PrincipalDep,
    run_id: Annotated[
        str | None, Query(max_length=100, description="one lab run; default every run")
    ] = None,
) -> Response:
    """Every tuning trial of the recorded lab runs: parameters, score and
    status, newest run first."""
    body = _service(services).lab_trials(principal, run_id)
    return _csv(body, "lab-trials", run_id or "all")

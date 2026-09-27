"""Reconciliation reports of your live portfolios (roadmap 19.5): each
check of a portfolio against its broker, with the drift it found."""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Path, Query

from stonks.api.deps import PrincipalDep, ServicesDep, needs
from stonks.api.errors import PROBLEM_RESPONSES
from stonks.app.reconcile import ReconcileReportView, ReconcileService
from stonks.auth import Permission

router = APIRouter(prefix="/api/reconcile", tags=["reconcile"], responses=PROBLEM_RESPONSES)


def _service(services: ServicesDep) -> ReconcileService:
    return ReconcileService(services.context)


@router.get(
    "/reports",
    response_model=list[ReconcileReportView],
    operation_id="listReconcileReports",
    dependencies=needs(Permission.READ),
)
def list_reconcile_reports(
    services: ServicesDep,
    principal: PrincipalDep,
    portfolio_id: Annotated[
        str | None, Query(max_length=64, description="one of your portfolios")
    ] = None,
    limit: Annotated[int, Query(ge=1, le=500)] = 50,
) -> list[ReconcileReportView]:
    """The latest checks of your live portfolios against their brokers,
    newest first. ``drift`` opened a ``broker_drift`` halt and paused auto.
    ``outage`` skipped the day. ``fault`` paused auto."""
    return _service(services).reports(principal, portfolio_id=portfolio_id, limit=limit)


@router.get(
    "/reports/{report_id}",
    response_model=ReconcileReportView,
    operation_id="getReconcileReport",
    dependencies=needs(Permission.READ),
)
def get_reconcile_report(
    report_id: Annotated[str, Path(max_length=64)],
    services: ServicesDep,
    principal: PrincipalDep,
) -> ReconcileReportView:
    """One check: every unexplained item, what it explained itself, and the
    owner's own positions and orders it kept apart."""
    return _service(services).report(principal, report_id)

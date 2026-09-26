from __future__ import annotations

from datetime import date

from fastapi import APIRouter

from stonks.api.deps import ServicesDep
from stonks.api.errors import PROBLEM_RESPONSES
from stonks.app.operations import PnlSeries

router = APIRouter(prefix="/api/pnl", tags=["pnl"], responses=PROBLEM_RESPONSES)


@router.get("", response_model=PnlSeries, operation_id="getPnl")
def get_pnl(services: ServicesDep, since: date | None = None) -> PnlSeries:
    """Daily P&L of the real portfolio (last snapshot per UTC day). Returns
    and drawdown are measured from inception even when ``since`` trims it."""
    return services.operations.pnl(since=since)

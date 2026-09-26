"""The statement audit's flags (BL-36): periods whose statements break an
accounting identity. Read-only; ``stonks audit statements`` writes them."""

from __future__ import annotations

from typing import Annotated, Literal

from fastapi import APIRouter, Depends, Query

from stonks.api.deps import PageDep, ServicesDep
from stonks.api.errors import PROBLEM_RESPONSES
from stonks.app.pagination import Page
from stonks.app.statements import StatementFlagView, StatementService

router = APIRouter(prefix="/api/statements", tags=["statements"], responses=PROBLEM_RESPONSES)


def get_statements(services: ServicesDep) -> StatementService:
    return StatementService(services.context)


StatementsDep = Annotated[StatementService, Depends(get_statements)]


@router.get("/flags", response_model=Page[StatementFlagView], operation_id="listStatementFlags")
def list_statement_flags(
    statements: StatementsDep,
    page: PageDep,
    ticker: Annotated[str | None, Query(max_length=32)] = None,
    severity: Literal["error", "warning"] | None = None,
) -> Page[StatementFlagView]:
    """Statement periods the audit flagged, by ticker, period and check.
    Lab preflight warns about ``error`` flags in a run's universe."""
    return statements.flags(ticker=ticker, severity=severity, limit=page.limit, offset=page.offset)

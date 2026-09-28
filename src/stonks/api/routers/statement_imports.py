"""CSV statement imports (roadmap 23.17): map a broker's CSV export onto
trades, dividends and cash flows, preview it, import it into the tables a
broker connection fills, and undo an import. Another person's import is a
404, admins included."""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Path

from stonks.api.deps import PageDep, PrincipalDep, ServicesDep, needs
from stonks.api.errors import PROBLEM_RESPONSES
from stonks.app.pagination import Page, page_of
from stonks.app.statement_imports import (
    StatementImportRequest,
    StatementImportService,
    StatementImportView,
    StatementPreview,
)
from stonks.auth import Permission

router = APIRouter(
    prefix="/api/statement-imports", tags=["statement-imports"], responses=PROBLEM_RESPONSES
)

ImportId = Annotated[str, Path(max_length=64)]


def _service(services: ServicesDep) -> StatementImportService:
    found = getattr(services, "statement_imports", None)
    return (
        found
        if isinstance(found, StatementImportService)
        else StatementImportService(services.context)
    )


@router.get("", response_model=Page[StatementImportView], operation_id="listStatementImports")
def list_statement_imports(
    services: ServicesDep, principal: PrincipalDep, page: PageDep
) -> Page[StatementImportView]:
    """Your CSV imports, newest first, undone ones included."""
    return page_of(_service(services).list(principal), page)


@router.post(
    "/preview",
    response_model=StatementPreview,
    operation_id="previewStatementImport",
    dependencies=needs(Permission.PORTFOLIO_MANAGE),
)
def preview_statement_import(
    body: StatementImportRequest, services: ServicesDep, principal: PrincipalDep
) -> StatementPreview:
    """Each row as new, duplicate or skipped, and the column mapping used
    (guessed from the headers when you give none). Writes nothing."""
    return _service(services).preview(principal, body)


@router.post(
    "",
    status_code=201,
    response_model=StatementImportView,
    operation_id="commitStatementImport",
    dependencies=needs(Permission.PORTFOLIO_MANAGE),
)
def commit_statement_import(
    body: StatementImportRequest, services: ServicesDep, principal: PrincipalDep
) -> StatementImportView:
    """Import the new rows (409 when every row is a duplicate or skipped)."""
    return _service(services).commit(principal, body)


@router.post(
    "/{import_id}/undo",
    response_model=StatementImportView,
    operation_id="undoStatementImport",
    dependencies=needs(Permission.PORTFOLIO_MANAGE),
)
def undo_statement_import(
    import_id: ImportId, services: ServicesDep, principal: PrincipalDep
) -> StatementImportView:
    """Remove exactly the rows this import added and rebuild the holdings."""
    return _service(services).undo(principal, import_id)

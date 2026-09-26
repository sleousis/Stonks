from __future__ import annotations

from fastapi import APIRouter

from stonks.api.deps import ServicesDep
from stonks.api.errors import PROBLEM_RESPONSES
from stonks.app.ingest import DataSourceInfo

router = APIRouter(prefix="/api/sources", tags=["sources"], responses=PROBLEM_RESPONSES)


@router.get("", response_model=list[DataSourceInfo], operation_id="listDataSources")
def list_sources(services: ServicesDep) -> list[DataSourceInfo]:
    """Data sources an ingest request can name, and whether each is configured."""
    return services.ingest.sources()

from __future__ import annotations

from importlib.metadata import PackageNotFoundError, version

from fastapi import APIRouter
from pydantic import BaseModel

router = APIRouter(prefix="/api/health", tags=["health"])


class Health(BaseModel):
    status: str
    version: str


def _version() -> str:
    try:
        return version("stonks")
    except PackageNotFoundError:  # pragma: no cover - source checkout without install
        return "0.0.0"


@router.get("", response_model=Health, operation_id="getHealth", summary="Liveness probe")
def health() -> Health:
    return Health(status="ok", version=_version())

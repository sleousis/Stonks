from __future__ import annotations

from typing import Literal

from fastapi import APIRouter
from pydantic import BaseModel

from stonks.api.errors import PROBLEM_RESPONSES

#: Mounted behind :func:`stonks.api.deps.require_token`: never open, not even
#: for reads on loopback.
router = APIRouter(prefix="/api/auth", tags=["auth"], responses=PROBLEM_RESPONSES)


class AuthCheck(BaseModel):
    authenticated: Literal[True] = True


@router.get("/check", response_model=AuthCheck, operation_id="checkAuth")
def check_auth() -> AuthCheck:
    """200 when the bearer token is valid, 401 otherwise (503 when the
    server has no token configured, as for every mutating route)."""
    return AuthCheck()

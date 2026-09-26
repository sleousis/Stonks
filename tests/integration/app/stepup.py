"""Test helper: treat every authenticated request as a browser session with a
fresh second factor, so tests of step-up routes (connect a broker, resume
the kill switch) can keep using a bearer token. Unauthenticated requests
still get 401."""

from __future__ import annotations

import dataclasses
from typing import Annotated

from fastapi import Depends, FastAPI, Request
from fastapi.security import HTTPAuthorizationCredentials

from stonks.api.deps import _bearer, current_principal
from stonks.auth import Principal


def _stepped_up(
    request: Request,
    creds: Annotated[HTTPAuthorizationCredentials | None, Depends(_bearer)],
) -> Principal:
    principal = current_principal(request, creds)
    return dataclasses.replace(principal, via="session", mfa_fresh=True)


def allow_step_up(app: FastAPI) -> FastAPI:
    app.dependency_overrides[current_principal] = _stepped_up
    return app

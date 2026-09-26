"""Auth errors. They are :class:`~stonks.app.errors.AppError` subclasses that
carry their own HTTP status (``http_status``) and headers, so the API maps
them without a router knowing about auth. Messages start with a stable code
(``mfa_required: ...``) that clients may match on."""

from __future__ import annotations

from typing import ClassVar

from stonks.app.errors import AppError, ConfigurationError


class AuthError(AppError):
    title = "Unauthorized"
    http_status: ClassVar[int] = 401
    code: ClassVar[str] = "unauthorized"

    def __init__(self, message: str = "", *, headers: dict[str, str] | None = None) -> None:
        super().__init__(f"{self.code}: {message}" if message else self.code)
        self.headers = headers


class NotAuthenticated(AuthError):
    """No credential, or one that is wrong, expired or revoked."""

    code = "not_authenticated"

    def __init__(self, message: str = "missing or invalid credentials") -> None:
        super().__init__(message, headers={"WWW-Authenticate": "Bearer"})


class InvalidCredentials(AuthError):
    """Wrong email, password or second-factor code (never says which)."""

    code = "invalid_credentials"


class MfaRequired(AuthError):
    """The session passed the password step but not the second factor."""

    code = "mfa_required"


class PermissionDenied(AuthError):
    title = "Forbidden"
    http_status = 403
    code = "forbidden"


class StepUpRequired(PermissionDenied):
    """Needs a second factor verified in the last few minutes, in the UI."""

    code = "step_up_required"


class CsrfFailed(PermissionDenied):
    code = "csrf_failed"


class TooManyAttempts(AuthError):
    title = "Too Many Requests"
    http_status = 429
    code = "too_many_attempts"

    def __init__(self, retry_after_seconds: int) -> None:
        super().__init__(
            "too many failed attempts; try again later",
            headers={"Retry-After": str(max(1, int(retry_after_seconds)))},
        )


class AuthNotConfigured(ConfigurationError):
    """A secret the auth layer needs (STONKS_API_TOKEN, STONKS_SECRET_KEYS) is missing."""

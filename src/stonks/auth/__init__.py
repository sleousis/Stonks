"""Auth and principals (roadmap 15.2 / 13.1 backend, step S2).

Passwords (argon2id), a mandatory TOTP second factor with recovery codes,
browser sessions with CSRF, per-user API tokens with scopes, one
:class:`Principal` per request and one policy table. See
``docs/design/accounts-and-modes.md`` sections 2 and 8 and
``docs/security.md``.
"""

from stonks.auth.errors import (
    AuthError,
    AuthNotConfigured,
    CsrfFailed,
    InvalidCredentials,
    MfaRequired,
    NotAuthenticated,
    PermissionDenied,
    StepUpRequired,
    TooManyAttempts,
)
from stonks.auth.passwords import PasswordHasher, PasswordPolicyError
from stonks.auth.policy import POLICY, Permission, Rule, allowed, require
from stonks.auth.principal import ROLE_SCOPES, WRITE_SCOPES, ApiScope, Principal
from stonks.auth.service import (
    ApiTokenInfo,
    AuthService,
    EnrolStart,
    LoginResult,
    MfaResult,
    NewSession,
    SessionInfo,
    UserAuthInfo,
)
from stonks.auth.settings import AuthSettings
from stonks.auth.totp import SecondFactor, Totp

__all__ = [
    "POLICY",
    "ROLE_SCOPES",
    "WRITE_SCOPES",
    "ApiScope",
    "ApiTokenInfo",
    "AuthError",
    "AuthNotConfigured",
    "AuthService",
    "AuthSettings",
    "CsrfFailed",
    "EnrolStart",
    "InvalidCredentials",
    "LoginResult",
    "MfaRequired",
    "MfaResult",
    "NewSession",
    "NotAuthenticated",
    "PasswordHasher",
    "PasswordPolicyError",
    "Permission",
    "PermissionDenied",
    "Principal",
    "Rule",
    "SecondFactor",
    "SessionInfo",
    "StepUpRequired",
    "TooManyAttempts",
    "Totp",
    "UserAuthInfo",
    "allowed",
    "require",
]

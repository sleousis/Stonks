"""Sign-in, second factor, API tokens and user administration.

Two routers:

- :data:`public_router` needs no principal: ``login`` opens a pending
  session, the second-factor routes work on that session cookie (pending or
  full) and ``logout`` ends it. They are the only way to turn a password
  into a principal.
- :data:`router` is mounted behind :func:`stonks.api.deps.require_token`:
  every route needs a principal, even reads on loopback.

The session cookie is ``HttpOnly; Secure; SameSite=Lax``. The CSRF token
comes back in the body and in a readable ``stonks_csrf`` cookie; send it as
``X-CSRF-Token`` on every unsafe request made with the cookie.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Literal

from fastapi import APIRouter, Depends, Request, Response
from pydantic import BaseModel, ConfigDict, Field

from stonks.accounts import Role
from stonks.api.deps import (
    CSRF_COOKIE,
    SESSION_COOKIE,
    AuthDep,
    PrincipalDep,
    SessionDep,
    needs,
    require_permission,
)
from stonks.api.errors import PROBLEM_RESPONSES, ProblemDetails
from stonks.auth import (
    ApiScope,
    ApiTokenInfo,
    AuthService,
    NewSession,
    NotAuthenticated,
    Permission,
    UserAuthInfo,
)

_RESPONSES = {
    **PROBLEM_RESPONSES,
    403: {"model": ProblemDetails, "description": "Forbidden"},
    429: {"model": ProblemDetails, "description": "Too Many Requests"},
}

public_router = APIRouter(prefix="/api/auth", tags=["auth"], responses=_RESPONSES)
router = APIRouter(prefix="/api/auth", tags=["auth"], responses=_RESPONSES)


# ---- models ---------------------------------------------------------------------


class _Secret(BaseModel):
    model_config = ConfigDict(hide_input_in_errors=True)


class LoginRequest(_Secret):
    email: str = Field(min_length=3, max_length=320)
    password: str = Field(min_length=1, max_length=1024)


class LoginView(BaseModel):
    user_id: str
    display_name: str
    #: ``enrol``: set up the authenticator app (first login). ``verify``: send a code.
    next_step: Literal["enrol", "verify"]
    csrf_token: str


class EnrolStartView(BaseModel):
    secret: str
    otpauth_uri: str


class MfaCodeRequest(_Secret):
    code: str | None = Field(default=None, max_length=16)
    recovery_code: str | None = Field(default=None, max_length=64)


class MfaView(BaseModel):
    method: Literal["totp", "recovery_code"]
    #: A new CSRF token when the sign-in completed (the session was replaced).
    csrf_token: str | None = None
    #: The ten recovery codes, only right after enrolment. Shown once.
    recovery_codes: list[str] | None = None
    recovery_codes_left: int


class MeView(BaseModel):
    user_id: str
    email: str | None
    display_name: str
    role: Role
    via: Literal["session", "token", "legacy", "cli", "scheduler"]
    scopes: list[ApiScope]
    mfa_enrolled: bool
    #: Second factor verified recently enough for sensitive actions.
    mfa_fresh: bool


class AuthCheck(BaseModel):
    authenticated: Literal[True] = True


class PasswordChangeRequest(_Secret):
    current_password: str = Field(min_length=1, max_length=1024)
    new_password: str = Field(min_length=1, max_length=1024)


class RecoveryCodesView(BaseModel):
    recovery_codes: list[str]


class TokenCreateRequest(BaseModel):
    name: str = Field(min_length=1, max_length=100)
    scopes: list[ApiScope] = Field(min_length=1)
    expires_in_days: int | None = Field(default=None, ge=1, le=365)


class TokenView(BaseModel):
    id: str
    name: str
    scopes: list[ApiScope]
    created_at: str
    last_used_at: str | None
    expires_at: str | None
    revoked_at: str | None


class TokenCreatedView(BaseModel):
    #: The whole token. Shown once; store it now.
    token: str
    info: TokenView


class UserView(BaseModel):
    """Identity and status only. Admins never see holdings here."""

    id: str
    email: str | None
    display_name: str
    role: Role
    status: Literal["active", "disabled"]
    mfa_enrolled: bool
    created_at: str
    last_login_at: str | None


class UserCreateRequest(_Secret):
    email: str = Field(min_length=3, max_length=320)
    display_name: str = Field(min_length=1, max_length=100)
    role: Role
    #: Initial password; the user sets up the second factor at first login.
    password: str = Field(min_length=1, max_length=1024)


class UserUpdateRequest(BaseModel):
    role: Role | None = None
    status: Literal["active", "disabled"] | None = None


class PasswordResetRequest(_Secret):
    new_password: str = Field(min_length=1, max_length=1024)


def _token_view(info: ApiTokenInfo) -> TokenView:
    return TokenView(
        id=info.id,
        name=info.name,
        scopes=list(info.scopes),
        created_at=info.created_at,
        last_used_at=info.last_used_at,
        expires_at=info.expires_at,
        revoked_at=info.revoked_at,
    )


def _user_view(info: UserAuthInfo) -> UserView:
    u = info.user
    return UserView(
        id=u.id,
        email=u.email,
        display_name=u.display_name,
        role=u.role,
        status=u.status,
        mfa_enrolled=info.mfa_enrolled,
        created_at=u.created_at,
        last_login_at=u.last_login_at,
    )


# ---- cookies --------------------------------------------------------------------


def _ip(request: Request) -> str | None:
    return request.client.host if request.client else None


def _set_cookies(response: Response, auth: AuthService, session: NewSession) -> None:
    max_age = max(1, int((session.expires_at - datetime.now(UTC)).total_seconds()))
    secure = auth.settings.cookie_secure
    response.set_cookie(
        SESSION_COOKIE,
        session.token,
        max_age=max_age,
        path="/",
        secure=secure,
        httponly=True,
        samesite="lax",
    )
    response.set_cookie(
        CSRF_COOKIE,
        session.csrf_token,
        max_age=max_age,
        path="/",
        secure=secure,
        httponly=False,
        samesite="lax",
    )


def _clear_cookies(response: Response, auth: AuthService) -> None:
    for name, http_only in ((SESSION_COOKIE, True), (CSRF_COOKIE, False)):
        response.delete_cookie(
            name, path="/", secure=auth.settings.cookie_secure, httponly=http_only, samesite="lax"
        )


# ---- sign-in (no principal yet) ---------------------------------------------------


@public_router.post("/login", response_model=LoginView, operation_id="login")
def login(body: LoginRequest, request: Request, response: Response, auth: AuthDep) -> LoginView:
    """Check email and password and open a pending session. The second
    factor is always required next (``next_step``). Five failures per 15
    minutes per account or per IP lock further attempts (429)."""
    result = auth.login(
        body.email, body.password, ip=_ip(request), user_agent=request.headers.get("user-agent")
    )
    _set_cookies(response, auth, result.session)
    return LoginView(
        user_id=result.user.id,
        display_name=result.user.display_name,
        next_step=result.next_step,
        csrf_token=result.session.csrf_token,
    )


@public_router.post("/mfa/enrol", response_model=EnrolStartView, operation_id="startMfaEnrolment")
def start_enrolment(session: SessionDep, auth: AuthDep) -> EnrolStartView:
    """A new authenticator secret (show it as a QR code of ``otpauth_uri``).
    Only while no second factor is set up."""
    start = auth.enrol_start(session)
    return EnrolStartView(secret=start.secret, otpauth_uri=start.otpauth_uri)


@public_router.post(
    "/mfa/enrol/confirm", response_model=MfaView, operation_id="confirmMfaEnrolment"
)
def confirm_enrolment(
    body: MfaCodeRequest, request: Request, response: Response, session: SessionDep, auth: AuthDep
) -> MfaView:
    """Confirm the authenticator with a code. Completes the sign-in and
    returns ten recovery codes, shown once."""
    result = auth.enrol_confirm(
        session,
        body.code or "",
        ip=_ip(request),
        user_agent=request.headers.get("user-agent"),
    )
    if result.session is not None:
        _set_cookies(response, auth, result.session)
    return MfaView(
        method=result.method,
        csrf_token=result.session.csrf_token if result.session else None,
        recovery_codes=result.recovery_codes,
        recovery_codes_left=result.recovery_codes_left,
    )


@public_router.post("/mfa/verify", response_model=MfaView, operation_id="verifyMfa")
def verify_mfa(
    body: MfaCodeRequest, request: Request, response: Response, session: SessionDep, auth: AuthDep
) -> MfaView:
    """Send a TOTP ``code`` or a ``recovery_code``. On a pending session
    this completes the sign-in; on a signed-in session it refreshes the
    step-up window for sensitive actions."""
    result = auth.verify_mfa(
        session,
        code=body.code,
        recovery_code=body.recovery_code,
        ip=_ip(request),
        user_agent=request.headers.get("user-agent"),
    )
    if result.session is not None:
        _set_cookies(response, auth, result.session)
    return MfaView(
        method=result.method,
        csrf_token=result.session.csrf_token if result.session else None,
        recovery_codes_left=result.recovery_codes_left,
    )


@public_router.post("/logout", status_code=204, operation_id="logout")
def logout(request: Request, response: Response, auth: AuthDep) -> Response:
    """End the browser session (safe to call when already signed out)."""
    try:
        session = auth.session(
            request.cookies.get(SESSION_COOKIE),
            csrf=request.headers.get("X-CSRF-Token"),
            unsafe=True,
        )
    except NotAuthenticated:
        session = None
    if session is not None:
        auth.logout(session.id_hash, actor=f"user:{session.user.id}", ip=_ip(request))
    response.status_code = 204
    _clear_cookies(response, auth)
    return response


# ---- the caller -------------------------------------------------------------------


@router.get("/check", response_model=AuthCheck, operation_id="checkAuth")
def check_auth() -> AuthCheck:
    """200 when the credential is valid, 401 otherwise (503 when a legacy
    token is sent but the server has none configured)."""
    return AuthCheck()


@router.get("/me", response_model=MeView, operation_id="getMe")
def me(principal: PrincipalDep, auth: AuthDep) -> MeView:
    """Who the credential belongs to, its scopes and second-factor state."""
    info = auth.me(principal)
    return MeView(
        user_id=info.user.id,
        email=info.user.email,
        display_name=info.user.display_name,
        role=info.user.role,
        via=principal.via,
        scopes=sorted(principal.scopes, key=list(ApiScope).index),
        mfa_enrolled=info.mfa_enrolled,
        mfa_fresh=principal.mfa_fresh,
    )


@router.post(
    "/password",
    status_code=204,
    operation_id="changePassword",
    dependencies=needs(Permission.PASSWORD_CHANGE),
)
def change_password(
    body: PasswordChangeRequest, request: Request, principal: PrincipalDep, auth: AuthDep
) -> Response:
    """Change your password (fresh second factor needed). Your other
    sessions are signed out."""
    auth.change_password(principal, body.current_password, body.new_password, ip=_ip(request))
    return Response(status_code=204)


@router.post(
    "/recovery-codes",
    response_model=RecoveryCodesView,
    operation_id="regenerateRecoveryCodes",
    dependencies=needs(Permission.RECOVERY_CODES),
)
def regenerate_recovery_codes(
    request: Request, principal: PrincipalDep, auth: AuthDep
) -> RecoveryCodesView:
    """Ten new recovery codes; the old ones stop working. Needs a fresh
    second factor."""
    return RecoveryCodesView(
        recovery_codes=auth.regenerate_recovery_codes(principal, ip=_ip(request))
    )


# ---- API tokens -------------------------------------------------------------------


@router.get("/tokens", response_model=list[TokenView], operation_id="listApiTokens")
def list_tokens(principal: PrincipalDep, auth: AuthDep) -> list[TokenView]:
    """Your API tokens (never the secret), revoked ones included."""
    return [_token_view(t) for t in auth.list_tokens(principal)]


@router.post(
    "/tokens",
    status_code=201,
    response_model=TokenCreatedView,
    operation_id="createApiToken",
    dependencies=needs(Permission.TOKENS_MANAGE),
)
def create_token(
    body: TokenCreateRequest, request: Request, principal: PrincipalDep, auth: AuthDep
) -> TokenCreatedView:
    """Create a token for scripts or the MCP server, from a signed-in
    session only. Scopes can't exceed your role; ``trade`` and ``admin``
    need a fresh second factor. The token is shown once."""
    info, token = auth.create_token(
        principal,
        name=body.name,
        scopes=body.scopes,
        expires_in_days=body.expires_in_days,
        ip=_ip(request),
    )
    return TokenCreatedView(token=token, info=_token_view(info))


@router.delete(
    "/tokens/{token_id}",
    status_code=204,
    operation_id="revokeApiToken",
    dependencies=needs(Permission.TOKENS_REVOKE),
)
def revoke_token(
    token_id: str, request: Request, principal: PrincipalDep, auth: AuthDep
) -> Response:
    """Revoke one of your tokens (another user's token is a 404)."""
    auth.revoke_token(principal, token_id, ip=_ip(request))
    return Response(status_code=204)


# ---- user administration (admins) ---------------------------------------------------

_users_read = Depends(require_permission(Permission.USERS_READ))


@router.get(
    "/users", response_model=list[UserView], operation_id="listUsers", dependencies=[_users_read]
)
def list_users(principal: PrincipalDep, auth: AuthDep) -> list[UserView]:
    """Every person with an account: identity, role, status. No holdings."""
    return [_user_view(u) for u in auth.list_users(principal)]


@router.post(
    "/users",
    status_code=201,
    response_model=UserView,
    operation_id="createUser",
    dependencies=needs(Permission.USERS_MANAGE),
)
def create_user(
    body: UserCreateRequest, request: Request, principal: PrincipalDep, auth: AuthDep
) -> UserView:
    """Add a person (fresh second factor needed). They set up their own
    second factor at first login."""
    info = auth.create_user(
        principal,
        email=body.email,
        display_name=body.display_name,
        role=body.role,
        password=body.password,
        ip=_ip(request),
    )
    return _user_view(info)


@router.patch(
    "/users/{user_id}",
    response_model=UserView,
    operation_id="updateUser",
    dependencies=needs(Permission.USERS_MANAGE),
)
def update_user(
    user_id: str,
    body: UserUpdateRequest,
    request: Request,
    principal: PrincipalDep,
    auth: AuthDep,
) -> UserView:
    """Change role or status. Disabling signs the person out everywhere and
    revokes their tokens. The last active admin stays an admin."""
    info = auth.update_user(principal, user_id, role=body.role, status=body.status, ip=_ip(request))
    return _user_view(info)


@router.post(
    "/users/{user_id}/password",
    status_code=204,
    operation_id="resetUserPassword",
    dependencies=needs(Permission.USERS_MANAGE),
)
def reset_user_password(
    user_id: str,
    body: PasswordResetRequest,
    request: Request,
    principal: PrincipalDep,
    auth: AuthDep,
) -> Response:
    """Set a new password for a person and sign them out."""
    auth.reset_password(principal, user_id, body.new_password, ip=_ip(request))
    return Response(status_code=204)


@router.delete(
    "/users/{user_id}/mfa",
    status_code=204,
    operation_id="resetUserMfa",
    dependencies=needs(Permission.USERS_MANAGE),
)
def reset_user_mfa(
    user_id: str, request: Request, principal: PrincipalDep, auth: AuthDep
) -> Response:
    """Clear a person's second factor (lost phone). They set it up again at
    the next login."""
    auth.reset_mfa(principal, user_id, ip=_ip(request))
    return Response(status_code=204)

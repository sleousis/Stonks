"""Login, second factor, sessions, API tokens and user administration.

Transport-neutral: the REST layer turns results into cookies and JSON, the
CLI (``python -m stonks.auth``) calls the bootstrap helpers. Every step that
matters is written to ``audit_log`` in the same transaction as the change.

Flow for a browser:

1. ``login`` checks the password and opens a *pending* session (no second
   factor yet). A pending session can only enrol or verify the second factor.
2. First login: ``enrol_start`` + ``enrol_confirm`` (TOTP is mandatory for
   every human, decision 2026-09-26). Later logins: ``verify_mfa`` with a
   TOTP code or a recovery code.
3. The pending session is replaced by a fresh one (new id and CSRF token),
   so an id seen before the second factor is worth nothing afterwards.

Scripts and MCP use per-user API tokens (``stk_<id>_<secret>``) created
from a signed-in session. ``STONKS_API_TOKEN`` keeps working as a token of
the bootstrap admin, with a deprecation warning.
"""

from __future__ import annotations

import hmac
import json
from collections.abc import Callable, Iterable, Iterator
from contextlib import AbstractContextManager, contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, Literal

from stonks.accounts import DEFAULT_OWNER_ID, AuditLog, NotFound, Role, User, UserRepository
from stonks.accounts.users import normalize_email
from stonks.app.errors import ConflictError, NotFoundError, ValidationError
from stonks.auth.credentials import (
    hash_secret,
    looks_like_api_token,
    mint_api_token,
    parse_api_token,
    random_secret,
)
from stonks.auth.errors import (
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
from stonks.auth.policy import Permission, require
from stonks.auth.principal import ROLE_SCOPES, ApiScope, Principal
from stonks.auth.recovery import generate_codes, hash_code
from stonks.auth.settings import AuthSettings
from stonks.auth.totp import Totp
from stonks.logging import get_logger
from stonks.security import SecretBox, SecretBoxError
from stonks.store.state import SqliteState

_log = get_logger("stonks.auth")

StateFactory = Callable[[], AbstractContextManager[SqliteState]]
Clock = Callable[[], datetime]
NextStep = Literal["enrol", "verify"]

#: ``last_seen_at`` / ``last_used_at`` are refreshed at most this often.
_TOUCH_EVERY = timedelta(seconds=60)


def _iso(ts: datetime) -> str:
    return ts.astimezone(UTC).isoformat(timespec="seconds")


def _parse(ts: str | None) -> datetime | None:
    return datetime.fromisoformat(ts) if ts else None


def _totp_aad(user_id: str) -> str:
    return f"users.totp_secret:{user_id}"


# ---- results ---------------------------------------------------------------


@dataclass(frozen=True)
class SessionInfo:
    id_hash: str
    user: User
    created_at: datetime
    mfa_verified_at: datetime | None

    @property
    def pending(self) -> bool:
        return self.mfa_verified_at is None


@dataclass(frozen=True)
class NewSession:
    """Secrets for the cookies; shown once, stored only as hashes."""

    token: str
    csrf_token: str
    expires_at: datetime


@dataclass(frozen=True)
class LoginResult:
    session: NewSession
    user: User
    next_step: NextStep


@dataclass(frozen=True)
class EnrolStart:
    secret: str
    otpauth_uri: str


@dataclass(frozen=True)
class MfaResult:
    #: Set when the pending session was replaced by a full one.
    session: NewSession | None
    #: Set on enrolment (the first ten codes) only.
    recovery_codes: list[str] | None
    method: Literal["totp", "recovery_code"]
    recovery_codes_left: int


@dataclass(frozen=True)
class ApiTokenInfo:
    id: str
    user_id: str
    name: str
    scopes: tuple[ApiScope, ...]
    created_at: str
    last_used_at: str | None
    expires_at: str | None
    revoked_at: str | None

    @classmethod
    def from_row(cls, row: Any) -> ApiTokenInfo:
        return cls(
            id=row["id"],
            user_id=row["user_id"],
            name=row["name"],
            scopes=tuple(ApiScope(s) for s in json.loads(row["scopes"])),
            created_at=row["created_at"],
            last_used_at=row["last_used_at"],
            expires_at=row["expires_at"],
            revoked_at=row["revoked_at"],
        )


@dataclass(frozen=True)
class UserAuthInfo:
    """A user plus second-factor status (no secrets, no holdings)."""

    user: User
    mfa_enrolled: bool


class AuthService:
    def __init__(
        self,
        state_factory: StateFactory,
        *,
        settings: AuthSettings | None = None,
        hasher: PasswordHasher | None = None,
        totp: Totp | None = None,
        box: SecretBox | None = None,
        legacy_token: Callable[[], str | None] = lambda: None,
        clock: Clock | None = None,
    ) -> None:
        self._state_factory = state_factory
        self.settings = settings or AuthSettings()
        self._hasher = hasher or PasswordHasher()
        self._totp = totp or Totp(issuer=self.settings.totp_issuer)
        self._box = box
        self._legacy_token = legacy_token
        self._clock = clock or (lambda: datetime.now(UTC))
        self._legacy_warned = False

    # ---- plumbing ------------------------------------------------------------

    @contextmanager
    def _state(self) -> Iterator[SqliteState]:
        with self._state_factory() as state:
            yield state

    def _now(self) -> datetime:
        return self._clock().astimezone(UTC)

    def _secret_box(self) -> SecretBox:
        if self._box is None:
            try:
                self._box = SecretBox.from_env()
            except SecretBoxError:
                raise AuthNotConfigured(
                    "second-factor secrets need an encryption key; set STONKS_SECRET_KEYS"
                ) from None
        return self._box

    @staticmethod
    def _auth_row(state: SqliteState, user_id: str) -> Any:
        rows = state.sql(
            "SELECT password_hash, totp_secret_enc, mfa_enrolled_at, totp_last_step"
            " FROM users WHERE id = ?",
            [user_id],
        )
        if not rows:
            raise NotFound(f"user {user_id!r} not found")
        return rows[0]

    # ---- rate limit ------------------------------------------------------------

    def _check_limit(self, state: SqliteState, email: str, ip: str | None) -> None:
        now = self._now()
        since = _iso(now - timedelta(minutes=self.settings.failure_window_minutes))
        # Per account, two counters. Password failures reset on any success.
        # Second-factor failures reset only on a second-factor success, so a
        # stolen password can't buy fresh code guesses by logging in again.
        checks = [
            (
                "SELECT created_at FROM login_attempts WHERE email = ? AND success = 0"
                " AND stage = 'password' AND created_at > ? AND id > COALESCE("
                "(SELECT MAX(id) FROM login_attempts WHERE email = ? AND success = 1), 0)"
                " ORDER BY id",
                [email, since, email],
            ),
            (
                "SELECT created_at FROM login_attempts WHERE email = ? AND success = 0"
                " AND stage = 'mfa' AND created_at > ? AND id > COALESCE("
                "(SELECT MAX(id) FROM login_attempts WHERE email = ? AND success = 1"
                " AND stage = 'mfa'), 0) ORDER BY id",
                [email, since, email],
            ),
        ]
        if ip:
            checks.append(
                (
                    "SELECT created_at FROM login_attempts WHERE ip = ? AND success = 0"
                    " AND created_at > ? ORDER BY id",
                    [ip, since],
                )
            )
        for query, params in checks:
            rows = state.sql(query, params)
            if len(rows) >= self.settings.max_failures:
                oldest = _parse(rows[-self.settings.max_failures]["created_at"]) or now
                reset = oldest + timedelta(minutes=self.settings.failure_window_minutes)
                raise TooManyAttempts(int((reset - now).total_seconds()) + 1)

    def _reserve(
        self, state: SqliteState, email: str, ip: str | None, stage: Literal["password", "mfa"]
    ) -> int:
        """Check the limits and record this attempt as a failure in one
        ``BEGIN IMMEDIATE`` transaction, before the slow check runs. Parallel
        requests are serialized here and each counts the others, so a burst
        never gets more than ``max_failures`` guesses (review finding AS-11).
        A success later flips the row with :meth:`_succeeded`."""
        state.con.execute("BEGIN IMMEDIATE")
        try:
            self._check_limit(state, email, ip)
            [row] = state.execute(
                "INSERT INTO login_attempts (email, ip, stage, success, created_at)"
                " VALUES (?, ?, ?, 0, ?) RETURNING id",
                [email, ip, stage, _iso(self._now())],
            ).fetchall()
            attempt_id = int(row[0])
        except BaseException:
            state.con.execute("ROLLBACK")
            raise
        state.con.execute("COMMIT")
        return attempt_id

    @staticmethod
    def _succeeded(state: SqliteState, attempt_id: int) -> None:
        state.execute("UPDATE login_attempts SET success = 1 WHERE id = ?", [attempt_id])

    @staticmethod
    def _release(state: SqliteState, attempt_id: int) -> None:
        """Forget a reserved attempt that never checked a secret (a refused
        request, not a guess)."""
        state.execute("DELETE FROM login_attempts WHERE id = ?", [attempt_id])

    # ---- sessions --------------------------------------------------------------

    def _open_session(
        self,
        state: SqliteState,
        user_id: str,
        *,
        mfa_verified: bool,
        ip: str | None,
        user_agent: str | None,
    ) -> NewSession:
        now = self._now()
        lifetime = (
            timedelta(days=self.settings.session_absolute_days)
            if mfa_verified
            else timedelta(minutes=self.settings.pending_login_minutes)
        )
        token, csrf = random_secret(), random_secret()
        state.execute(
            "INSERT INTO sessions (id_hash, user_id, csrf_hash, created_at, last_seen_at,"
            " expires_at, mfa_verified_at, ip, user_agent) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            [
                hash_secret(token),
                user_id,
                hash_secret(csrf),
                _iso(now),
                _iso(now),
                _iso(now + lifetime),
                _iso(now) if mfa_verified else None,
                ip,
                (user_agent or "")[:256] or None,
            ],
        )
        return NewSession(token=token, csrf_token=csrf, expires_at=now + lifetime)

    def session(self, token: str | None, *, csrf: str | None = None, unsafe: bool) -> SessionInfo:
        """The live session behind a cookie value (pending or full). Unsafe
        methods also need the CSRF header of that session."""
        if not token:
            raise NotAuthenticated()
        now = self._now()
        with self._state() as state:
            rows = state.sql(
                "SELECT * FROM sessions WHERE id_hash = ? AND revoked_at IS NULL",
                [hash_secret(token)],
            )
            if not rows:
                raise NotAuthenticated("session expired or signed out")
            row = rows[0]
            last_seen = _parse(row["last_seen_at"]) or now
            idle = timedelta(hours=self.settings.session_idle_hours)
            if now >= (_parse(row["expires_at"]) or now) or now - last_seen >= idle:
                raise NotAuthenticated("session expired or signed out")
            try:
                user = UserRepository(state).get(row["user_id"])
            except NotFound:
                raise NotAuthenticated("session expired or signed out") from None
            if user.status != "active" or user.kind != "human":
                raise NotAuthenticated("session expired or signed out")
            if unsafe and not (csrf and hmac.compare_digest(hash_secret(csrf), row["csrf_hash"])):
                raise CsrfFailed("missing or wrong X-CSRF-Token header")
            if now - last_seen >= _TOUCH_EVERY:
                state.execute(
                    "UPDATE sessions SET last_seen_at = ? WHERE id_hash = ?",
                    [_iso(now), row["id_hash"]],
                )
        return SessionInfo(
            id_hash=row["id_hash"],
            user=user,
            created_at=_parse(row["created_at"]) or now,
            mfa_verified_at=_parse(row["mfa_verified_at"]),
        )

    def principal_for_session(
        self, token: str | None, *, csrf: str | None = None, unsafe: bool
    ) -> Principal:
        info = self.session(token, csrf=csrf, unsafe=unsafe)
        if info.pending:
            with self._state() as state:
                enrolled = self._auth_row(state, info.user.id)["mfa_enrolled_at"] is not None
            raise MfaRequired(
                "finish signing in with your second factor",
                next_step="verify" if enrolled else "enrol",
            )
        window = timedelta(minutes=self.settings.step_up_minutes)
        fresh = info.mfa_verified_at is not None and self._now() - info.mfa_verified_at < window
        return Principal.create(
            user_id=info.user.id,
            kind=info.user.kind,
            role=info.user.role,
            scopes=ROLE_SCOPES[info.user.role],
            mfa_fresh=fresh,
            via="session",
            credential_id=info.id_hash,
        )

    # ---- bearer tokens ---------------------------------------------------------

    def principal_for_bearer(self, token: str) -> Principal:
        if looks_like_api_token(token):
            return self._principal_for_api_token(token)
        return self._principal_for_legacy(token)

    def _principal_for_api_token(self, token: str) -> Principal:
        token_id = parse_api_token(token)
        if token_id is None:
            raise NotAuthenticated()
        now = self._now()
        with self._state() as state:
            rows = state.sql(
                "SELECT * FROM api_tokens WHERE id = ? AND revoked_at IS NULL", [token_id]
            )
            if not rows or not hmac.compare_digest(rows[0]["token_hash"], hash_secret(token)):
                raise NotAuthenticated()
            row = rows[0]
            expires = _parse(row["expires_at"])
            if expires is not None and now >= expires:
                raise NotAuthenticated("token expired")
            user = UserRepository(state).get(row["user_id"])
            if user.status != "active":
                raise NotAuthenticated()
            last_used = _parse(row["last_used_at"])
            if last_used is None or now - last_used >= _TOUCH_EVERY:
                state.execute(
                    "UPDATE api_tokens SET last_used_at = ? WHERE id = ?", [_iso(now), token_id]
                )
        return Principal.create(
            user_id=user.id,
            kind=user.kind,
            role=user.role,
            scopes=json.loads(row["scopes"]),
            mfa_fresh=False,
            via="token",
            credential_id=token_id,
        )

    def _principal_for_legacy(self, token: str) -> Principal:
        configured = self._legacy_token()
        if not configured:
            raise AuthNotConfigured("API token not configured; set STONKS_API_TOKEN")
        if not hmac.compare_digest(token.encode("utf-8"), configured.encode("utf-8")):
            raise NotAuthenticated()
        if not self._legacy_warned:
            self._legacy_warned = True
            _log.warning(
                "auth.legacy_token_used",
                detail="STONKS_API_TOKEN acts as the bootstrap admin; "
                "switch to a personal token (stk_...) before it is removed",
            )
        with self._state() as state:
            try:
                user = UserRepository(state).get(DEFAULT_OWNER_ID)
            except NotFound:
                user = None
        if user is None or user.status != "active":
            raise AuthNotConfigured(
                "the bootstrap admin is missing or disabled; run `stonks db init`"
            )
        return Principal.create(
            user_id=user.id,
            kind=user.kind,
            role=user.role,
            scopes=ROLE_SCOPES[user.role],
            mfa_fresh=False,
            via="legacy",
        )

    # ---- login and logout ------------------------------------------------------

    def login(
        self, email: str, password: str, *, ip: str | None = None, user_agent: str | None = None
    ) -> LoginResult:
        key = normalize_email(email or "")
        with self._state() as state:
            attempt = self._reserve(state, key, ip, "password")
            try:
                user = UserRepository(state).get_by_email(key) if key else None
            except NotFound:
                user = None
            stored = self._auth_row(state, user.id)["password_hash"] if user else None
            ok = self._hasher.verify(stored, password or "")
            if not ok or user is None or user.status != "active" or user.kind != "human":
                if user is not None:
                    AuditLog(state).record(
                        f"user:{user.id}", "auth.login_failed", "user", user.id, ip=ip
                    )
                raise InvalidCredentials("wrong email or password")
            enrolled = self._auth_row(state, user.id)["mfa_enrolled_at"] is not None
            with state.transaction():
                self._succeeded(state, attempt)
                if stored and self._hasher.needs_rehash(stored):
                    state.execute(
                        "UPDATE users SET password_hash = ? WHERE id = ?",
                        [self._hasher.hash(password), user.id],
                    )
                session = self._open_session(
                    state, user.id, mfa_verified=False, ip=ip, user_agent=user_agent
                )
                AuditLog(state).record(
                    f"user:{user.id}",
                    "auth.login.password",
                    "user",
                    user.id,
                    details={"next": "verify" if enrolled else "enrol"},
                    ip=ip,
                )
        return LoginResult(session=session, user=user, next_step="verify" if enrolled else "enrol")

    def logout(self, session_hash: str, *, actor: str, ip: str | None = None) -> None:
        with self._state() as state, state.transaction():
            row = state.sql("SELECT user_id FROM sessions WHERE id_hash = ?", [session_hash])
            state.execute(
                "UPDATE sessions SET revoked_at = ? WHERE id_hash = ? AND revoked_at IS NULL",
                [_iso(self._now()), session_hash],
            )
            if row:
                AuditLog(state).record(actor, "auth.logout", "user", row[0]["user_id"], ip=ip)

    def _revoke_all(self, state: SqliteState, user_id: str, *, keep: str | None = None) -> None:
        now = _iso(self._now())
        state.execute(
            "UPDATE sessions SET revoked_at = ? WHERE user_id = ? AND revoked_at IS NULL"
            " AND id_hash IS NOT ?",
            [now, user_id, keep],
        )

    # ---- second factor -------------------------------------------------------

    def enrol_start(self, info: SessionInfo) -> EnrolStart:
        """A new TOTP secret for the authenticator app. Allowed only while
        no factor is enrolled (an admin reset clears it)."""
        box = self._secret_box()
        with self._state() as state:
            if self._auth_row(state, info.user.id)["mfa_enrolled_at"] is not None:
                raise ConflictError("a second factor is already set up")
            secret = self._totp.new_secret()
            sealed = box.seal(secret.encode(), aad=_totp_aad(info.user.id))
            state.execute(
                "UPDATE users SET totp_secret_enc = ?, totp_last_step = NULL WHERE id = ?",
                [sealed.token, info.user.id],
            )
        account = info.user.email or info.user.id
        return EnrolStart(secret=secret, otpauth_uri=self._totp.provisioning_uri(secret, account))

    def enrol_confirm(
        self,
        info: SessionInfo,
        code: str,
        *,
        ip: str | None = None,
        user_agent: str | None = None,
    ) -> MfaResult:
        box = self._secret_box()
        key = (info.user.email or info.user.id).lower()
        with self._state() as state:
            attempt = self._reserve(state, key, ip, "mfa")
            row = self._auth_row(state, info.user.id)
            if row["mfa_enrolled_at"] is not None:
                self._release(state, attempt)
                raise ConflictError("a second factor is already set up")
            if row["totp_secret_enc"] is None:
                self._release(state, attempt)
                raise ConflictError("start the set-up first")
            secret = self._open_secret(box, row["totp_secret_enc"], info.user.id)
            step = self._totp.verify(secret, code, last_step=None, at=self._now())
            if step is None:
                raise InvalidCredentials("wrong code")
            codes = generate_codes()
            now = _iso(self._now())
            with state.transaction():
                self._succeeded(state, attempt)
                state.execute(
                    "UPDATE users SET mfa_enrolled_at = ?, totp_last_step = ? WHERE id = ?",
                    [now, step, info.user.id],
                )
                self._store_codes(state, info.user.id, codes)
                session = self._complete(state, info, ip=ip, user_agent=user_agent)
                AuditLog(state).record(
                    f"user:{info.user.id}",
                    "auth.mfa.enrol",
                    "user",
                    info.user.id,
                    details={"factor": "totp"},
                    ip=ip,
                )
        return MfaResult(
            session=session,
            recovery_codes=codes,
            method="totp",
            recovery_codes_left=len(codes),
        )

    def verify_mfa(
        self,
        info: SessionInfo,
        *,
        code: str | None = None,
        recovery_code: str | None = None,
        ip: str | None = None,
        user_agent: str | None = None,
    ) -> MfaResult:
        """Second step of a login, or a step-up on a full session."""
        if bool(code) == bool(recovery_code):
            raise ValidationError("send exactly one of code or recovery_code")
        key = (info.user.email or info.user.id).lower()
        with self._state() as state:
            attempt = self._reserve(state, key, ip, "mfa")
            row = self._auth_row(state, info.user.id)
            if row["mfa_enrolled_at"] is None:
                self._release(state, attempt)
                raise ConflictError("no second factor is set up yet; enrol first")
            method: Literal["totp", "recovery_code"]
            if code:
                box = self._secret_box()
                secret = self._open_secret(box, row["totp_secret_enc"], info.user.id)
                step = self._totp.verify(
                    secret, code, last_step=row["totp_last_step"], at=self._now()
                )
                # Claim the step atomically: of two parallel checks of one
                # code, only the one whose update lands is accepted.
                if step is not None:
                    claimed = state.execute(
                        "UPDATE users SET totp_last_step = ? WHERE id = ?"
                        " AND (totp_last_step IS NULL OR totp_last_step < ?)",
                        [step, info.user.id, step],
                    )
                    if claimed.rowcount != 1:
                        step = None
                ok, method = step is not None, "totp"
            else:
                code_hash = hash_code(recovery_code or "")
                used = state.execute(
                    "UPDATE recovery_codes SET used_at = ? WHERE user_id = ? AND code_hash = ?"
                    " AND used_at IS NULL",
                    [_iso(self._now()), info.user.id, code_hash],
                )
                ok, method, step = used.rowcount == 1, "recovery_code", None
            if not ok:
                AuditLog(state).record(
                    f"user:{info.user.id}",
                    "auth.mfa.failed",
                    "user",
                    info.user.id,
                    details={"method": method},
                    ip=ip,
                )
                raise InvalidCredentials("wrong code")
            with state.transaction():
                self._succeeded(state, attempt)
                session = self._complete(state, info, ip=ip, user_agent=user_agent)
                left = self._codes_left(state, info.user.id)
                AuditLog(state).record(
                    f"user:{info.user.id}",
                    "auth.mfa.verify",
                    "user",
                    info.user.id,
                    details={"method": method, "step_up": not info.pending},
                    ip=ip,
                )
        return MfaResult(
            session=session, recovery_codes=None, method=method, recovery_codes_left=left
        )

    def _complete(
        self, state: SqliteState, info: SessionInfo, *, ip: str | None, user_agent: str | None
    ) -> NewSession | None:
        """Pending session: replace it with a full one. Full session: mark
        the second factor as fresh (step-up)."""
        now = _iso(self._now())
        if not info.pending:
            state.execute(
                "UPDATE sessions SET mfa_verified_at = ? WHERE id_hash = ?", [now, info.id_hash]
            )
            return None
        state.execute("UPDATE sessions SET revoked_at = ? WHERE id_hash = ?", [now, info.id_hash])
        state.execute("UPDATE users SET last_login_at = ? WHERE id = ?", [now, info.user.id])
        return self._open_session(
            state, info.user.id, mfa_verified=True, ip=ip, user_agent=user_agent
        )

    def _open_secret(self, box: SecretBox, sealed: str | None, user_id: str) -> str:
        if not sealed:
            raise ConflictError("no second factor is set up yet; enrol first")
        try:
            return box.open(sealed, aad=_totp_aad(user_id)).decode()
        except SecretBoxError:
            raise AuthNotConfigured(
                "the second-factor secret cannot be opened; check STONKS_SECRET_KEYS"
            ) from None

    def _store_codes(self, state: SqliteState, user_id: str, codes: Iterable[str]) -> None:
        state.execute("DELETE FROM recovery_codes WHERE user_id = ?", [user_id])
        now = _iso(self._now())
        for code in codes:
            state.execute(
                "INSERT INTO recovery_codes (user_id, code_hash, created_at) VALUES (?, ?, ?)",
                [user_id, hash_code(code), now],
            )

    @staticmethod
    def _codes_left(state: SqliteState, user_id: str) -> int:
        row = state.sql(
            "SELECT COUNT(*) AS n FROM recovery_codes WHERE user_id = ? AND used_at IS NULL",
            [user_id],
        )
        return int(row[0]["n"])

    def regenerate_recovery_codes(
        self, principal: Principal, *, ip: str | None = None
    ) -> list[str]:
        self._require_step_up(principal)
        codes = generate_codes()
        with self._state() as state, state.transaction():
            if self._auth_row(state, principal.user_id)["mfa_enrolled_at"] is None:
                raise ConflictError("no second factor is set up yet")
            self._store_codes(state, principal.user_id, codes)
            AuditLog(state).record(
                principal.actor, "auth.recovery_codes.regenerate", "user", principal.user_id, ip=ip
            )
        return codes

    @staticmethod
    def _require_step_up(principal: Principal) -> None:
        if principal.via != "session" or not principal.mfa_fresh:
            raise StepUpRequired("confirm your second factor in the web app first")

    # ---- the signed-in user ----------------------------------------------------

    def is_active_user(self, user_id: str) -> bool:
        """True while ``user_id`` exists and is not disabled (stream tokens)."""
        with self._state() as state:
            rows = state.sql("SELECT status FROM users WHERE id = ?", [user_id])
        return bool(rows) and rows[0]["status"] == "active"

    def me(self, principal: Principal) -> UserAuthInfo:
        with self._state() as state:
            user = UserRepository(state).get(principal.user_id)
            enrolled = self._auth_row(state, user.id)["mfa_enrolled_at"] is not None
        return UserAuthInfo(user=user, mfa_enrolled=enrolled)

    def change_password(
        self, principal: Principal, current: str, new: str, *, ip: str | None = None
    ) -> None:
        """Signs out every other session of the user."""
        require(principal, Permission.PASSWORD_CHANGE)
        with self._state() as state:
            stored = self._auth_row(state, principal.user_id)["password_hash"]
            if not self._hasher.verify(stored, current or ""):
                raise InvalidCredentials("current password is wrong")
            new_hash = self._hash(new)
            with state.transaction():
                state.execute(
                    "UPDATE users SET password_hash = ?, password_changed_at = ? WHERE id = ?",
                    [new_hash, _iso(self._now()), principal.user_id],
                )
                self._revoke_all(state, principal.user_id, keep=principal.credential_id)
                AuditLog(state).record(
                    principal.actor, "auth.password.change", "user", principal.user_id, ip=ip
                )

    def _hash(self, password: str) -> str:
        try:
            return self._hasher.hash(password)
        except PasswordPolicyError as exc:
            raise ValidationError(str(exc)) from None

    # ---- API tokens ------------------------------------------------------------

    def create_token(
        self,
        principal: Principal,
        *,
        name: str,
        scopes: Iterable[ApiScope | str],
        expires_in_days: int | None = None,
        ip: str | None = None,
    ) -> tuple[ApiTokenInfo, str]:
        """``(info, token)``; the token is shown once. Tokens come from a
        signed-in session only; ``trade`` and ``admin`` need a step-up."""
        require(principal, Permission.TOKENS_MANAGE)
        wanted = frozenset(ApiScope(s) for s in scopes)
        if not wanted:
            raise ValidationError("a token needs at least one scope")
        extra = wanted - ROLE_SCOPES[principal.role]
        if extra:
            raise PermissionDenied(
                "scopes beyond your role: " + ", ".join(sorted(s.value for s in extra))
            )
        if wanted & {ApiScope.TRADE, ApiScope.ADMIN}:
            self._require_step_up(principal)
        if not (name or "").strip():
            raise ValidationError("name must not be blank")
        token_id, token = mint_api_token()
        now = self._now()
        expires = now + timedelta(days=expires_in_days) if expires_in_days else None
        ordered = sorted((s.value for s in wanted), key=[s.value for s in ApiScope].index)
        with self._state() as state, state.transaction():
            state.execute(
                "INSERT INTO api_tokens (id, user_id, name, token_hash, scopes, created_at,"
                " expires_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
                [
                    token_id,
                    principal.user_id,
                    name.strip(),
                    hash_secret(token),
                    json.dumps(ordered),
                    _iso(now),
                    _iso(expires) if expires else None,
                ],
            )
            AuditLog(state).record(
                principal.actor,
                "auth.token.create",
                "api_token",
                token_id,
                details={"scopes": ordered, "expires_at": _iso(expires) if expires else None},
                ip=ip,
            )
            info = ApiTokenInfo.from_row(
                state.sql("SELECT * FROM api_tokens WHERE id = ?", [token_id])[0]
            )
        return info, token

    def list_tokens(self, principal: Principal) -> list[ApiTokenInfo]:
        with self._state() as state:
            rows = state.sql(
                "SELECT * FROM api_tokens WHERE user_id = ? ORDER BY created_at, id",
                [principal.user_id],
            )
        return [ApiTokenInfo.from_row(r) for r in rows]

    def revoke_token(self, principal: Principal, token_id: str, *, ip: str | None = None) -> None:
        """Another user's token reads as not found. Any credential of the
        owner may revoke (a leaked token can revoke itself)."""
        with self._state() as state, state.transaction():
            rows = state.sql(
                "SELECT revoked_at FROM api_tokens WHERE id = ? AND user_id = ?",
                [token_id, principal.user_id],
            )
            if not rows:
                raise NotFoundError(f"token {token_id!r} not found")
            if rows[0]["revoked_at"] is None:
                state.execute(
                    "UPDATE api_tokens SET revoked_at = ? WHERE id = ?",
                    [_iso(self._now()), token_id],
                )
                AuditLog(state).record(
                    principal.actor, "auth.token.revoke", "api_token", token_id, ip=ip
                )

    # ---- user administration (admins, step-up) ---------------------------------

    def list_users(self, principal: Principal) -> list[UserAuthInfo]:
        """Identity and status only: admins never see holdings here."""
        require(principal, Permission.USERS_READ)
        with self._state() as state:
            users = UserRepository(state).list_all()
            enrolled = {
                r["id"] for r in state.sql("SELECT id FROM users WHERE mfa_enrolled_at IS NOT NULL")
            }
        return [UserAuthInfo(u, u.id in enrolled) for u in users if u.kind == "human"]

    def create_user(
        self,
        principal: Principal,
        *,
        email: str,
        display_name: str,
        role: Role,
        password: str,
        ip: str | None = None,
    ) -> UserAuthInfo:
        require(principal, Permission.USERS_MANAGE)
        return self._create_user(
            principal.actor, email=email, display_name=display_name, role=role, password=password
        )

    def _create_user(
        self, actor: str, *, email: str, display_name: str, role: Role, password: str
    ) -> UserAuthInfo:
        email = normalize_email(email or "")
        if "@" not in email:
            raise ValidationError("a valid email is required")
        password_hash = self._hash(password)
        with self._state() as state, state.transaction():
            repo = UserRepository(state)
            try:
                repo.get_by_email(email)
            except NotFound:
                pass
            else:
                raise ConflictError("a user with that email already exists")
            user = repo.create(display_name=display_name, role=Role(role), actor=actor, email=email)
            state.execute(
                "UPDATE users SET password_hash = ?, password_changed_at = ? WHERE id = ?",
                [password_hash, _iso(self._now()), user.id],
            )
        return UserAuthInfo(user=user, mfa_enrolled=False)

    def update_user(
        self,
        principal: Principal,
        user_id: str,
        *,
        role: Role | None = None,
        status: Literal["active", "disabled"] | None = None,
        ip: str | None = None,
    ) -> UserAuthInfo:
        """Change role or status. Disabling revokes every session and token
        (the repository also pauses auto subscriptions). The last active
        admin can't be demoted or disabled."""
        require(principal, Permission.USERS_MANAGE)
        return self._update_user(principal.actor, user_id, role=role, status=status, ip=ip)

    def _update_user(
        self,
        actor: str,
        user_id: str,
        *,
        role: Role | None = None,
        status: Literal["active", "disabled"] | None = None,
        ip: str | None = None,
    ) -> UserAuthInfo:
        with self._state() as state, state.transaction():
            repo = UserRepository(state)
            user = self._human(repo, user_id)
            losing_admin = user.role is Role.ADMIN and (
                (role is not None and Role(role) is not Role.ADMIN) or status == "disabled"
            )
            if losing_admin and user.status == "active":
                admins = state.sql(
                    "SELECT COUNT(*) AS n FROM users WHERE role = 'admin' AND status = 'active'"
                    " AND kind = 'human'"
                )[0]["n"]
                if admins <= 1:
                    raise ConflictError("the last active admin can't be demoted or disabled")
            if role is not None and Role(role) is not user.role:
                state.execute("UPDATE users SET role = ? WHERE id = ?", [Role(role).value, user_id])
                AuditLog(state).record(
                    actor,
                    "user.role",
                    "user",
                    user_id,
                    details={"from": user.role.value, "to": Role(role).value},
                    ip=ip,
                )
            if status is not None and status != user.status:
                repo.set_status(user_id, status, actor=actor)
                if status == "disabled":
                    self._revoke_all(state, user_id)
                    state.execute(
                        "UPDATE api_tokens SET revoked_at = ? WHERE user_id = ?"
                        " AND revoked_at IS NULL",
                        [_iso(self._now()), user_id],
                    )
            updated = repo.get(user_id)
            enrolled = self._auth_row(state, user_id)["mfa_enrolled_at"] is not None
        return UserAuthInfo(user=updated, mfa_enrolled=enrolled)

    def reset_password(
        self, principal: Principal, user_id: str, new_password: str, *, ip: str | None = None
    ) -> None:
        """Admin sets a new password. The user's sessions are signed out and
        their API tokens revoked (a reset usually means a suspected leak)."""
        require(principal, Permission.USERS_MANAGE)
        new_hash = self._hash(new_password)
        with self._state() as state, state.transaction():
            self._human(UserRepository(state), user_id)
            self._set_password(state, user_id, new_hash)
            self._revoke_tokens(state, user_id)
            AuditLog(state).record(principal.actor, "auth.password.reset", "user", user_id, ip=ip)

    def reset_mfa(self, principal: Principal, user_id: str, *, ip: str | None = None) -> None:
        """Clear a user's second factor (lost phone and codes). They enrol
        again at the next login. Sessions and API tokens are revoked."""
        require(principal, Permission.USERS_MANAGE)
        self._reset_mfa(principal.actor, user_id, ip=ip)

    def _reset_mfa(self, actor: str, user_id: str, *, ip: str | None = None) -> None:
        with self._state() as state, state.transaction():
            self._human(UserRepository(state), user_id)
            state.execute(
                "UPDATE users SET totp_secret_enc = NULL, mfa_enrolled_at = NULL,"
                " totp_last_step = NULL WHERE id = ?",
                [user_id],
            )
            state.execute("DELETE FROM recovery_codes WHERE user_id = ?", [user_id])
            self._revoke_all(state, user_id)
            self._revoke_tokens(state, user_id)
            AuditLog(state).record(actor, "auth.mfa.reset", "user", user_id, ip=ip)

    @staticmethod
    def _human(repo: UserRepository, user_id: str) -> User:
        try:
            user = repo.get(user_id)
        except NotFound:
            raise NotFoundError(f"user {user_id!r} not found") from None
        if user.kind != "human":
            raise NotFoundError(f"user {user_id!r} not found")
        return user

    def _revoke_tokens(self, state: SqliteState, user_id: str) -> None:
        state.execute(
            "UPDATE api_tokens SET revoked_at = ? WHERE user_id = ? AND revoked_at IS NULL",
            [_iso(self._now()), user_id],
        )

    def _set_password(self, state: SqliteState, user_id: str, password_hash: str) -> None:
        state.execute(
            "UPDATE users SET password_hash = ?, password_changed_at = ? WHERE id = ?",
            [password_hash, _iso(self._now()), user_id],
        )
        self._revoke_all(state, user_id)

    # ---- bootstrap (CLI, shell access implies admin) ---------------------------

    def bootstrap_admin(self, email: str, password: str, *, actor: str = "service:cli") -> User:
        """Give the bootstrap admin (``usr_owner``) an email and a password so
        the first login works. Refused once it already has a password (use
        :meth:`set_password_by_email` to reset)."""
        new_hash = self._hash(password)
        email = normalize_email(email or "")
        if "@" not in email:
            raise ValidationError("a valid email is required")
        with self._state() as state, state.transaction():
            repo = UserRepository(state)
            try:
                owner = repo.get(DEFAULT_OWNER_ID)
            except NotFound:
                raise ConflictError("run `stonks db init` first") from None
            if self._auth_row(state, owner.id)["password_hash"] is not None:
                raise ConflictError("the bootstrap admin already has a password; reset it instead")
            try:
                other = repo.get_by_email(email)
            except NotFound:
                other = None
            if other is not None and other.id != owner.id:
                raise ConflictError("another user already has that email")
            state.execute("UPDATE users SET email = ? WHERE id = ?", [email, owner.id])
            self._set_password(state, owner.id, new_hash)
            AuditLog(state).record(actor, "auth.bootstrap", "user", owner.id)
            return repo.get(owner.id)

    # Shell access implies admin: these act for the operator, by email, and
    # skip the step-up a browser session needs (there is no session).

    def create_user_from_shell(
        self,
        *,
        email: str,
        display_name: str,
        role: Role,
        password: str,
        actor: str = "service:cli",
    ) -> UserAuthInfo:
        return self._create_user(
            actor, email=email, display_name=display_name, role=role, password=password
        )

    def update_user_from_shell(
        self,
        email: str,
        *,
        role: Role | None = None,
        status: Literal["active", "disabled"] | None = None,
        actor: str = "service:cli",
    ) -> UserAuthInfo:
        return self._update_user(actor, self._id_by_email(email), role=role, status=status)

    def reset_mfa_from_shell(self, email: str, *, actor: str = "service:cli") -> str:
        """Clear a person's second factor from the shell: the way back in for
        a sole admin who lost the authenticator and the recovery codes."""
        user_id = self._id_by_email(email)
        self._reset_mfa(actor, user_id)
        return user_id

    def _id_by_email(self, email: str) -> str:
        with self._state() as state:
            try:
                return UserRepository(state).get_by_email(normalize_email(email or "")).id
            except NotFound:
                raise NotFoundError("no user with that email") from None

    def set_password_by_email(
        self, email: str, password: str, *, actor: str = "service:cli"
    ) -> User:
        new_hash = self._hash(password)
        with self._state() as state, state.transaction():
            try:
                user = UserRepository(state).get_by_email(email)
            except NotFound:
                raise NotFoundError("no user with that email") from None
            self._set_password(state, user.id, new_hash)
            AuditLog(state).record(actor, "auth.password.reset", "user", user.id)
            return user

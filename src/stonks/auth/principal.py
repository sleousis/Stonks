"""Who is acting. Every entry point (browser session, API token, the legacy
env token, CLI, scheduler) resolves to one :class:`Principal`; app services
receive it and ask :mod:`stonks.auth.policy` whether it may act."""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from enum import StrEnum
from typing import Literal

from stonks.accounts import Role, Scope
from stonks.accounts.models import UserKind


class ApiScope(StrEnum):
    """What a credential may do. A token never exceeds its user's role."""

    READ = "read"
    TRADE = "trade"  # orders, modes (not enabling auto), kill switch on
    LAB = "lab"  # jobs, backtests, lab runs
    ADMIN = "admin"
    #: A remote lab worker (``python -m stonks.lab.offload worker --api``):
    #: claims queued lab jobs and uploads their results. Admins only. A token
    #: that holds only this scope reaches the lab worker routes and nothing
    #: else (see :data:`CONFINED_SCOPES`).
    LAB_WORKER = "lab_worker"


ROLE_SCOPES: dict[Role, frozenset[ApiScope]] = {
    Role.VIEWER: frozenset({ApiScope.READ}),
    Role.TRADER: frozenset({ApiScope.READ, ApiScope.TRADE, ApiScope.LAB}),
    Role.ADMIN: frozenset(ApiScope),
}

#: Scopes that allow any unsafe HTTP method (the method-keyed floor).
WRITE_SCOPES = frozenset({ApiScope.TRADE, ApiScope.LAB, ApiScope.ADMIN, ApiScope.LAB_WORKER})

#: Machine scopes: a credential holding only these may call just the routes
#: whose permission one of them grants, reads included.
CONFINED_SCOPES = frozenset({ApiScope.LAB_WORKER})

#: ``assistant`` and ``telegram``: a signed-in user acting through the in-app
#: assistant or a linked Telegram chat. Like a token, they never pass step-up.
Via = Literal["session", "token", "legacy", "cli", "scheduler", "assistant", "telegram"]


@dataclass(frozen=True)
class Principal:
    user_id: str
    kind: UserKind
    role: Role
    scopes: frozenset[ApiScope]
    #: A second factor was verified within the step-up window (sessions only).
    mfa_fresh: bool
    via: Via
    #: Session hash or token id; lets logout and revocation find the credential.
    credential_id: str | None = None

    @classmethod
    def create(
        cls,
        *,
        user_id: str,
        kind: UserKind,
        role: Role,
        scopes: Iterable[ApiScope | str],
        mfa_fresh: bool,
        via: Via,
        credential_id: str | None = None,
    ) -> Principal:
        """Build a principal whose scopes are clamped to what ``role`` allows."""
        role = Role(role)
        granted = frozenset(ApiScope(s) for s in scopes) & ROLE_SCOPES[role]
        return cls(user_id, kind, role, granted, mfa_fresh and via == "session", via, credential_id)

    @classmethod
    def service(cls, name: str) -> Principal:
        """In-process service principal (``scheduler``, ``system``)."""
        scope = Scope.service(name)
        return cls(
            user_id=scope.user_id,
            kind="service",
            role=Role.ADMIN,
            scopes=frozenset({ApiScope.READ, ApiScope.TRADE, ApiScope.LAB}),
            mfa_fresh=False,
            via="scheduler",
        )

    @property
    def scope(self) -> Scope:
        """The data scope used by repositories (tenant isolation)."""
        return Scope(user_id=self.user_id, role=self.role, kind=self.kind)

    @property
    def actor(self) -> str:
        """Written to every audit row: ``user:<id>`` or ``service:<name>``."""
        return self.scope.actor

    def has(self, scope: ApiScope) -> bool:
        return scope in self.scopes

    @property
    def can_write(self) -> bool:
        return bool(self.scopes & WRITE_SCOPES)

    @property
    def confined(self) -> bool:
        """True for a machine credential (only :data:`CONFINED_SCOPES`)."""
        return bool(self.scopes) and self.scopes <= CONFINED_SCOPES

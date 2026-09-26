"""Who is asking, and the one ownership check for portfolio-scoped state.

SQLite has no row-level security, so tenant isolation lives here. A
:class:`Scope` is the part of a principal the data layer needs (step S2's
``Principal`` carries one). Every portfolio-scoped repository call starts
with :func:`owned_portfolio`, which returns the row or raises
:class:`NotFound`, never a "forbidden", so ids of other users' rows don't
leak.

Visibility (decision 2026-09-26): a human sees only their own portfolios,
admins included (admins get aggregate totals through dedicated services,
never individual books). A disabled user sees nothing. Service scopes
(``service:scheduler``, ``service:system``) act in-process for every book.
"""

from __future__ import annotations

from dataclasses import dataclass

from stonks.accounts.models import NotFound, Portfolio, Role, User, UserKind
from stonks.store.state import SqliteState

_SERVICE_PREFIX = "svc_"


@dataclass(frozen=True)
class Scope:
    user_id: str
    role: Role
    kind: UserKind = "human"

    def __post_init__(self) -> None:
        # A service scope reaches every book; it must never be mistaken for
        # (or built from) a person's id.
        if (self.kind == "service") != self.user_id.startswith(_SERVICE_PREFIX):
            raise ValueError(f"inconsistent scope: kind={self.kind!r}, user_id={self.user_id!r}")
        object.__setattr__(self, "role", Role(self.role))

    @classmethod
    def for_user(cls, user: User) -> Scope:
        return cls(user_id=user.id, role=user.role, kind=user.kind)

    @classmethod
    def service(cls, name: str) -> Scope:
        """An in-process service principal (``scheduler``, ``system``)."""
        if not name or not name.isidentifier():
            raise ValueError(f"bad service name {name!r}")
        return cls(user_id=f"{_SERVICE_PREFIX}{name}", role=Role.ADMIN, kind="service")

    @property
    def is_service(self) -> bool:
        return self.kind == "service"

    @property
    def actor(self) -> str:
        """Written to every audit row: ``user:<id>`` or ``service:<name>``."""
        if self.is_service:
            return f"service:{self.user_id.removeprefix(_SERVICE_PREFIX)}"
        return f"user:{self.user_id}"


def owned_portfolio(state: SqliteState, scope: Scope, portfolio_id: str) -> Portfolio:
    """The portfolio ``portfolio_id`` if ``scope`` may act on it, else
    :class:`NotFound` (missing and not-yours read the same)."""
    if scope.is_service:
        rows = state.sql("SELECT * FROM portfolios WHERE id = ?", [portfolio_id])
    else:
        rows = state.sql(
            "SELECT p.* FROM portfolios p JOIN users u ON u.id = p.owner_id"
            " WHERE p.id = ? AND p.owner_id = ? AND u.status = 'active'",
            [portfolio_id, scope.user_id],
        )
    if not rows:
        raise NotFound(f"portfolio {portfolio_id!r} not found")
    return Portfolio.from_row(rows[0])

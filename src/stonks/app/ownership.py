"""Owner checks for "global, attributed" rows (jobs, Studio drafts).

A row records who made it (``owner_id``). The owner sees and changes it;
admins with the ``admin`` scope and in-process services see every row.
Anyone else gets "not found", so ids never leak (review finding AS-06).
``principal=None`` is an in-process caller or a loopback read in the dev
profile (``open_reads_on_loopback``), which see everything.
"""

from __future__ import annotations

from stonks.app.errors import NotFoundError
from stonks.auth.policy import Permission, allowed
from stonks.auth.principal import Principal


def sees_all(principal: Principal | None) -> bool:
    return (
        principal is None
        or principal.scope.is_service
        or allowed(principal, Permission.OPERATIONS_RUN)
    )


def owner_filter(principal: Principal | None) -> str | None:
    """The owner id a listing must be limited to, or ``None`` for all rows."""
    return None if sees_all(principal) else principal.user_id  # type: ignore[union-attr]


def check_owner(owner_id: str | None, principal: Principal | None, message: str) -> None:
    """Raise ``NotFoundError`` unless ``principal`` may see a row owned by ``owner_id``."""
    if sees_all(principal):
        return
    assert principal is not None
    if owner_id != principal.user_id:
        raise NotFoundError(message)


def owner_of(principal: Principal | None) -> str | None:
    """The owner id to record for a row ``principal`` creates."""
    if principal is None or principal.scope.is_service:
        return None
    return principal.user_id

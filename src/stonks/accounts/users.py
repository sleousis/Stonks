"""``users`` repository. Passwords, second factors and sessions arrive with
step S2 (``auth/``); this layer keeps identity, role and status. Permission
checks (who may create or disable users) live in S2's policy, not here."""

from __future__ import annotations

import uuid

from stonks.accounts.audit import AuditLog, iso_now
from stonks.accounts.models import NotFound, Role, User, UserKind, UserStatus
from stonks.store.state import SqliteState


def normalize_email(email: str) -> str:
    """The one spelling of an email we store and look up: trimmed and
    lowercased. (The column is also ``COLLATE NOCASE``, so rows written
    before this rule still match.)"""
    return email.strip().lower()


class UserRepository:
    def __init__(self, state: SqliteState) -> None:
        self._state = state
        self._audit = AuditLog(state)

    def get(self, user_id: str) -> User:
        rows = self._state.sql("SELECT * FROM users WHERE id = ?", [user_id])
        if not rows:
            raise NotFound(f"user {user_id!r} not found")
        return User.from_row(rows[0])

    def get_by_email(self, email: str) -> User:
        rows = self._state.sql("SELECT * FROM users WHERE email = ?", [normalize_email(email)])
        if not rows:
            raise NotFound("user not found")
        return User.from_row(rows[0])

    def list_all(self) -> list[User]:
        """Every user (admin services only)."""
        return [User.from_row(r) for r in self._state.sql("SELECT * FROM users ORDER BY id")]

    def create(
        self,
        *,
        display_name: str,
        role: Role,
        actor: str,
        email: str | None = None,
        kind: UserKind = "human",
        timezone: str = "UTC",
    ) -> User:
        user_id = f"usr_{uuid.uuid4().hex[:12]}"
        with self._state.transaction():
            self._state.execute(
                "INSERT INTO users (id, kind, email, display_name, role, timezone, created_at)"
                " VALUES (?, ?, ?, ?, ?, ?, ?)",
                [
                    user_id,
                    kind,
                    normalize_email(email) if email else None,
                    display_name,
                    Role(role).value,
                    timezone,
                    iso_now(),
                ],
            )
            self._audit.record(
                actor, "user.create", "user", user_id, details={"role": Role(role).value}
            )
        return self.get(user_id)

    def set_status(self, user_id: str, status: UserStatus, *, actor: str) -> User:
        """Disabling a user pauses their auto subscriptions (reason
        ``user_disabled``); S2 also revokes their sessions and tokens."""
        before = self.get(user_id)
        with self._state.transaction():
            self._state.execute("UPDATE users SET status = ? WHERE id = ?", [status, user_id])
            if status == "disabled":
                self._state.execute(
                    "UPDATE subscriptions SET paused_reason = 'user_disabled', updated_at = ?"
                    " WHERE user_id = ? AND mode = 'auto' AND paused_reason IS NULL",
                    [iso_now(), user_id],
                )
            self._audit.record(
                actor,
                "user.status",
                "user",
                user_id,
                details={"from": before.status, "to": status},
            )
        return self.get(user_id)

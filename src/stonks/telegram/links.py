"""One-time link codes and chat links (``telegram_link_codes``,
``telegram_links``, migration 028).

- A code is 8 characters from an alphabet without look-alikes. Only its
  SHA-256 is stored. It works once, before it expires, and making a new
  code retires the user's older unused ones.
- A chat is linked to exactly one user and a user to at most one chat.
  Linking a user again moves them to the new chat. A chat already linked
  to someone else is refused until it is unlinked.
- Linking and unlinking write an ``audit_log`` row.
"""

from __future__ import annotations

import hashlib
import secrets
import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from stonks.accounts.audit import AuditLog
from stonks.store.state import SqliteState

#: No 0/O, 1/I/L: the code is read off a screen and typed on a phone.
ALPHABET = "ABCDEFGHJKMNPQRSTUVWXYZ23456789"
CODE_LENGTH = 8


class LinkError(ValueError):
    """A code is unknown, used or expired, or the chat belongs to someone else."""


@dataclass(frozen=True)
class ChatLink:
    chat_id: str
    user_id: str
    username: str | None
    linked_at: datetime


def _iso(dt: datetime) -> str:
    return dt.astimezone(UTC).isoformat(timespec="seconds")


def _now() -> datetime:
    return datetime.now(UTC)


def normalize_code(code: str) -> str:
    return "".join(code.split()).upper().replace("-", "")


def hash_code(code: str) -> str:
    return hashlib.sha256(normalize_code(code).encode()).hexdigest()


def _link(r: sqlite3.Row) -> ChatLink:
    return ChatLink(
        chat_id=r["chat_id"],
        user_id=r["user_id"],
        username=r["username"],
        linked_at=datetime.fromisoformat(r["linked_at"]),
    )


class LinkStore:
    def __init__(self, state: SqliteState) -> None:
        self._state = state

    # ---- codes ----------------------------------------------------------------------

    def create_code(
        self, user_id: str, *, minutes: float, actor: str, now: datetime | None = None
    ) -> tuple[str, datetime]:
        """A fresh code for ``user_id`` and when it expires. Older unused
        codes of the user stop working."""
        now = now or _now()
        code = "".join(secrets.choice(ALPHABET) for _ in range(CODE_LENGTH))
        expires = now + timedelta(minutes=minutes)
        with self._state.transaction():
            self._state.execute(
                "UPDATE telegram_link_codes SET used_at = ? WHERE user_id = ? AND used_at IS NULL",
                [_iso(now), user_id],
            )
            self._state.execute(
                "INSERT INTO telegram_link_codes (code_hash, user_id, created_at, expires_at)"
                " VALUES (?, ?, ?, ?)",
                [hash_code(code), user_id, _iso(now), _iso(expires)],
            )
            AuditLog(self._state).record(actor, "telegram.link_code", "user", user_id)
        return code, expires

    def redeem(
        self, code: str, chat_id: str, username: str | None, *, now: datetime | None = None
    ) -> ChatLink:
        """Link ``chat_id`` to the owner of ``code`` (single use). Raises
        :class:`LinkError` for a bad code or a chat of someone else."""
        now = now or _now()
        digest = hash_code(code)
        with self._state.transaction():
            rows = self._state.sql(
                "SELECT user_id FROM telegram_link_codes WHERE code_hash = ?"
                " AND used_at IS NULL AND expires_at > ?",
                [digest, _iso(now)],
            )
            if not rows:
                raise LinkError("that code is unknown, used or expired; make a new one")
            user_id = rows[0]["user_id"]
            current = self.for_chat(chat_id)
            if current is not None and current.user_id != user_id:
                raise LinkError("this chat is linked to another account; send /unlink first")
            claimed = self._state.execute(
                "UPDATE telegram_link_codes SET used_at = ? WHERE code_hash = ? AND used_at IS NULL",
                [_iso(now), digest],
            )
            if claimed.rowcount != 1:
                raise LinkError("that code is unknown, used or expired; make a new one")
            self._state.execute("DELETE FROM telegram_links WHERE user_id = ?", [user_id])
            self._state.execute("DELETE FROM telegram_links WHERE chat_id = ?", [chat_id])
            self._state.execute(
                "INSERT INTO telegram_links (chat_id, user_id, username, linked_at)"
                " VALUES (?, ?, ?, ?)",
                [chat_id, user_id, username, _iso(now)],
            )
            AuditLog(self._state).record(
                f"user:{user_id}",
                "telegram.link",
                "telegram_chat",
                chat_id,
                details={"username": username},
            )
        return ChatLink(chat_id, user_id, username, now.replace(microsecond=0))

    # ---- links ----------------------------------------------------------------------

    def for_chat(self, chat_id: str) -> ChatLink | None:
        rows = self._state.sql("SELECT * FROM telegram_links WHERE chat_id = ?", [chat_id])
        return _link(rows[0]) if rows else None

    def for_user(self, user_id: str) -> ChatLink | None:
        rows = self._state.sql("SELECT * FROM telegram_links WHERE user_id = ?", [user_id])
        return _link(rows[0]) if rows else None

    def unlink_user(self, user_id: str, *, actor: str, reason: str = "user") -> bool:
        with self._state.transaction():
            link = self.for_user(user_id)
            if link is None:
                return False
            self._state.execute("DELETE FROM telegram_links WHERE user_id = ?", [user_id])
            AuditLog(self._state).record(
                actor, "telegram.unlink", "telegram_chat", link.chat_id, details={"reason": reason}
            )
        return True

    def unlink_chat(self, chat_id: str, *, actor: str, reason: str = "user") -> bool:
        link = self.for_chat(chat_id)
        if link is None:
            return False
        return self.unlink_user(link.user_id, actor=actor, reason=reason)

    # ---- bot state ------------------------------------------------------------------

    def get_offset(self) -> int | None:
        rows = self._state.sql("SELECT value FROM telegram_bot_state WHERE key = 'update_offset'")
        return int(rows[0]["value"]) if rows else None

    def set_offset(self, offset: int) -> None:
        self._state.execute(
            "INSERT INTO telegram_bot_state (key, value) VALUES ('update_offset', ?)"
            " ON CONFLICT (key) DO UPDATE SET value = excluded.value",
            [str(offset)],
        )

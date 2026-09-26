"""Per-user notification preferences and settings (quiet hours, webhook).

Resolution for (user, category, strategy, channel): the most specific row
wins, ``(category, strategy)`` over ``(category, any strategy)``, else the
channel's default. The in-app feed isn't configurable: it always gets a row.

This is the storage layer and takes a bare ``user_id``; the scoped entry
points (who may change whose settings) are in :mod:`stonks.notify.service`.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime

from stonks.notify.events import CATEGORIES, Category
from stonks.notify.quiet import QuietHours, parse_hhmm
from stonks.store.state import SqliteState


@dataclass(frozen=True)
class Preference:
    category: Category
    channel: str
    enabled: bool
    strategy_id: str | None = None

    def __post_init__(self) -> None:
        if self.category not in CATEGORIES:
            raise ValueError(f"unknown category {self.category!r}")
        if not self.channel or not self.channel.isidentifier():
            raise ValueError(f"bad channel name {self.channel!r}")


@dataclass(frozen=True)
class UserNotifySettings:
    user_id: str
    timezone: str
    quiet_start: str | None
    quiet_end: str | None
    webhook_url: str | None

    @property
    def quiet_hours(self) -> QuietHours | None:
        if not self.quiet_start or not self.quiet_end:
            return None
        return QuietHours(parse_hhmm(self.quiet_start), parse_hhmm(self.quiet_end), self.timezone)


def _iso(now: datetime) -> str:
    return now.isoformat(timespec="seconds")


class PreferenceStore:
    def __init__(self, state: SqliteState) -> None:
        self._state = state

    def list(self, user_id: str) -> list[Preference]:
        rows = self._state.sql(
            "SELECT category, channel, enabled, strategy_id FROM notification_prefs"
            " WHERE user_id = ? ORDER BY category, COALESCE(strategy_id, ''), channel",
            [user_id],
        )
        return [
            Preference(r["category"], r["channel"], bool(r["enabled"]), r["strategy_id"])
            for r in rows
        ]

    def set(self, user_id: str, prefs: Iterable[Preference], *, now: datetime) -> None:
        with self._state.transaction():
            for p in prefs:
                self._delete(user_id, p.category, p.channel, p.strategy_id)
                self._state.execute(
                    "INSERT INTO notification_prefs (user_id, category, strategy_id, channel,"
                    " enabled, updated_at) VALUES (?, ?, ?, ?, ?, ?)",
                    [user_id, p.category, p.strategy_id, p.channel, int(p.enabled), _iso(now)],
                )

    def reset(self, user_id: str, category: str, channel: str, strategy_id: str | None) -> None:
        """Drop one row, so the next less specific rule applies again."""
        self._delete(user_id, category, channel, strategy_id)

    def _delete(self, user_id: str, category: str, channel: str, strategy_id: str | None) -> None:
        self._state.execute(
            "DELETE FROM notification_prefs WHERE user_id = ? AND category = ? AND channel = ?"
            " AND COALESCE(strategy_id, '') = COALESCE(?, '')",
            [user_id, category, channel, strategy_id],
        )

    def explicit(
        self, user_id: str, category: str, channel: str, strategy_id: str | None = None
    ) -> bool | None:
        """The user's own choice for this combination, or None if they made none."""
        rows = self._state.sql(
            "SELECT enabled, strategy_id FROM notification_prefs"
            " WHERE user_id = ? AND category = ? AND channel = ?"
            " AND (strategy_id IS NULL OR strategy_id = ?)"
            " ORDER BY strategy_id IS NULL",  # the strategy-specific row first
            [user_id, category, channel, strategy_id],
        )
        return bool(rows[0]["enabled"]) if rows else None

    def enabled(
        self,
        user_id: str,
        category: str,
        channel: str,
        strategy_id: str | None = None,
        *,
        default: bool,
    ) -> bool:
        choice = self.explicit(user_id, category, channel, strategy_id)
        return default if choice is None else choice

    # ---- settings -------------------------------------------------------------

    def settings(self, user_id: str) -> UserNotifySettings:
        rows = self._state.sql(
            "SELECT u.id, u.timezone, s.quiet_start, s.quiet_end, s.webhook_url"
            " FROM users u LEFT JOIN notification_settings s ON s.user_id = u.id"
            " WHERE u.id = ?",
            [user_id],
        )
        if not rows:
            return UserNotifySettings(user_id, "UTC", None, None, None)
        r = rows[0]
        return UserNotifySettings(
            user_id, r["timezone"] or "UTC", r["quiet_start"], r["quiet_end"], r["webhook_url"]
        )

    def set_quiet_hours(
        self, user_id: str, start: str | None, end: str | None, *, now: datetime
    ) -> None:
        if (start is None) != (end is None):
            raise ValueError("quiet hours need both a start and an end, or neither")
        if start is not None and end is not None:
            parse_hhmm(start)
            parse_hhmm(end)
        self._upsert(user_id, now, quiet_start=start, quiet_end=end)

    def set_webhook(self, user_id: str, url: str | None, *, now: datetime) -> None:
        self._upsert(user_id, now, webhook_url=url)

    def _upsert(self, user_id: str, now: datetime, **values: str | None) -> None:
        cols = ", ".join(values)
        marks = ", ".join("?" for _ in values)
        updates = ", ".join(f"{c} = excluded.{c}" for c in values)
        self._state.execute(
            f"INSERT INTO notification_settings (user_id, {cols}, updated_at)"
            f" VALUES (?, {marks}, ?) ON CONFLICT(user_id) DO UPDATE SET {updates},"
            " updated_at = excluded.updated_at",
            [user_id, *values.values(), _iso(now)],
        )

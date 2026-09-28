"""Per-user notification preferences and settings (quiet hours, webhook).

Resolution for (user, category, strategy, channel): the most specific row
wins, ``(category, strategy)`` over ``(category, any strategy)``, else the
channel's default. The in-app feed isn't configurable: it always gets a row.

Upcoming-event alerts also have one switch per kind (earnings, dividends,
economic releases) in :class:`EventAlertPrefStore`. Off means the person
gets no alert of that kind at all, not even in the app. Every kind is on
until the person turns it off. Which channels carry the kinds left on is
the ``event_alert`` category's channel preference, like any other category.

Economic release alerts add two choices in :class:`EconomicAlertPrefStore`:
the countries (default: those of the person's portfolios' base currencies,
else US) and the lowest importance that alerts (default: high).

This is the storage layer and takes a bare ``user_id``; the scoped entry
points (who may change whose settings) are in :mod:`stonks.notify.service`.
"""

from __future__ import annotations

import json
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime

from stonks.calendars.countries import FALLBACK_COUNTRY, country_for_currency, is_country_code
from stonks.calendars.importance import IMPORTANCE_LEVELS, Importance
from stonks.notify.events import CATEGORIES, Category
from stonks.notify.quiet import QuietHours, parse_hhmm
from stonks.store.state import SqliteState

#: Each event alert switch in plain words, in display order. An
#: ``EventAlertKind`` names one of these as its ``topic``. An open set:
#: a new topic is one entry here, with no table change.
EVENT_ALERT_TOPICS: dict[str, str] = {
    "earnings": "Earnings coming up",
    "dividends": "Ex-dividend dates coming up",
    "economic": "Economic releases coming up",
}


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


class EventAlertPrefStore:
    """Per-person switches for the upcoming-event alert kinds. No row means on."""

    def __init__(self, state: SqliteState) -> None:
        self._state = state

    def switches(self, user_id: str) -> dict[str, bool]:
        """Every topic, in display order, with the person's choice or on."""
        rows = self._state.sql(
            "SELECT topic, enabled FROM event_alert_prefs WHERE user_id = ?", [user_id]
        )
        chosen = {r["topic"]: bool(r["enabled"]) for r in rows}
        return {topic: chosen.get(topic, True) for topic in EVENT_ALERT_TOPICS}

    def enabled(self, user_id: str, topic: str) -> bool:
        rows = self._state.sql(
            "SELECT enabled FROM event_alert_prefs WHERE user_id = ? AND topic = ?",
            [user_id, topic],
        )
        return bool(rows[0]["enabled"]) if rows else True

    def set(self, user_id: str, switches: Mapping[str, bool], *, now: datetime) -> None:
        unknown = sorted(set(switches) - set(EVENT_ALERT_TOPICS))
        if unknown:
            raise ValueError(
                f"unknown event alert kind {unknown[0]!r}; choose from {list(EVENT_ALERT_TOPICS)}"
            )
        with self._state.transaction():
            for topic, enabled in switches.items():
                self._state.execute(
                    "INSERT INTO event_alert_prefs (user_id, topic, enabled, updated_at)"
                    " VALUES (?, ?, ?, ?) ON CONFLICT(user_id, topic) DO UPDATE SET"
                    " enabled = excluded.enabled, updated_at = excluded.updated_at",
                    [user_id, topic, int(bool(enabled)), _iso(now)],
                )


#: At most this many countries per person.
ECONOMIC_COUNTRIES_MAX = 30


@dataclass(frozen=True)
class EconomicAlertPrefs:
    """A person's economic release alert choices, defaults filled in."""

    #: The countries alerts go out for, in order.
    countries: tuple[str, ...]
    #: True while the countries follow the person's portfolios.
    countries_default: bool
    #: The lowest importance that alerts.
    min_importance: Importance


def default_countries(state: SqliteState, user_id: str) -> tuple[str, ...]:
    """The countries of the base currencies of a person's portfolios that
    are not archived, oldest portfolio first, else US."""
    rows = state.sql(
        "SELECT base_currency FROM portfolios WHERE owner_id = ? AND status != 'archived'"
        " ORDER BY created_at, id",
        [user_id],
    )
    found = [country_for_currency(r["base_currency"] or "") for r in rows]
    countries = tuple(dict.fromkeys(c for c in found if c))
    return countries or (FALLBACK_COUNTRY,)


def clean_countries(countries: Sequence[str]) -> tuple[str, ...]:
    """Upper case, each once, in the order given. ``ValueError`` for a bad
    code, an empty list or too many."""
    out = tuple(dict.fromkeys(c.strip().upper() for c in countries))
    if not out:
        raise ValueError("pick at least one country, or turn economic releases off")
    if len(out) > ECONOMIC_COUNTRIES_MAX:
        raise ValueError(f"pick at most {ECONOMIC_COUNTRIES_MAX} countries")
    for code in out:
        if not is_country_code(code):
            raise ValueError(f"bad country code {code!r}; use two letters such as US or DE")
    return out


class EconomicAlertPrefStore:
    """Countries and importance threshold for economic release alerts. No
    row means the defaults."""

    def __init__(self, state: SqliteState) -> None:
        self._state = state

    def get(self, user_id: str) -> EconomicAlertPrefs:
        rows = self._state.sql(
            "SELECT countries_json, min_importance FROM economic_alert_prefs WHERE user_id = ?",
            [user_id],
        )
        raw = rows[0]["countries_json"] if rows else None
        threshold: Importance = rows[0]["min_importance"] if rows else "high"
        if raw is None:
            return EconomicAlertPrefs(default_countries(self._state, user_id), True, threshold)
        return EconomicAlertPrefs(tuple(json.loads(raw)), False, threshold)

    def set(
        self,
        user_id: str,
        *,
        countries: Sequence[str] | None = None,
        default_countries: bool = False,
        min_importance: str | None = None,
        now: datetime,
    ) -> None:
        """Change what is given. ``countries`` picks them, ``default_countries``
        goes back to following the portfolios. ``ValueError`` on bad input."""
        if countries is not None and default_countries:
            raise ValueError("pick countries or go back to the default, not both")
        if min_importance is not None and min_importance not in IMPORTANCE_LEVELS:
            raise ValueError(
                f"unknown importance {min_importance!r}; choose from {list(IMPORTANCE_LEVELS)}"
            )
        picked = clean_countries(countries) if countries is not None else None
        if picked is None and not default_countries and min_importance is None:
            return
        rows = self._state.sql(
            "SELECT countries_json, min_importance FROM economic_alert_prefs WHERE user_id = ?",
            [user_id],
        )
        stored = rows[0]["countries_json"] if rows else None
        if picked is not None:
            stored = json.dumps(list(picked))
        elif default_countries:
            stored = None
        threshold = min_importance or (rows[0]["min_importance"] if rows else "high")
        self._state.execute(
            "INSERT INTO economic_alert_prefs (user_id, countries_json, min_importance,"
            " updated_at) VALUES (?, ?, ?, ?) ON CONFLICT(user_id) DO UPDATE SET"
            " countries_json = excluded.countries_json,"
            " min_importance = excluded.min_importance, updated_at = excluded.updated_at",
            [user_id, stored, threshold, _iso(now)],
        )

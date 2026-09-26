"""NotificationRouter: turns one :class:`Event` into per-user outbox rows.

For every active human user in the event's audience, inside the caller's
transaction (so producers enqueue atomically with the event itself):

1. **Dedupe.** A user gets a given ``dedupe_key`` once: a unique index on
   ``(user_id, dedupe_key)`` over active rows makes a re-run tick send
   nothing twice, even from two processes. With a dedupe window, keys older
   than the window are retired first so the event can fire again.
2. **In-app.** An ``alerts`` row (the user's feed), always, whatever the
   preferences or quiet hours.
3. **Outbox.** A ``notification_outbox`` row with the minimal payload.
4. **Deliveries.** One ``notification_deliveries`` row per enabled channel
   and target (each push device). ``high`` urgency with no working push
   device falls back to the fallback channels (webhook, email) unless the
   user turned them off for that category.

Quiet hours are applied at delivery time by the worker, so a change to a
user's quiet hours affects everything still queued. Title and body are
redacted (configured secrets, credential query params, bearer tokens)
before anything is stored.
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Literal, get_args
from urllib.parse import urlencode

from stonks.logging import get_logger
from stonks.notify.channels import Channel
from stonks.notify.events import Audience, Event, Urgency
from stonks.notify.prefs import PreferenceStore
from stonks.notify.settings import NotifySettings, OutboxSettings
from stonks.notify.store import redact_text
from stonks.store.state import SqliteState

_log = get_logger("stonks.notify.router")

Clock = Callable[[], datetime]
SignalKind = Literal["entry", "exit", "increase", "decrease", "risk"]
SIGNAL_KINDS: tuple[str, ...] = get_args(SignalKind)


def utcnow() -> datetime:
    return datetime.now(UTC)


def iso(dt: datetime) -> str:
    """The one timestamp format of the outbox: UTC, seconds, ``+00:00``, so
    string comparison is time comparison."""
    return dt.astimezone(UTC).isoformat(timespec="seconds")


@dataclass(frozen=True)
class PublishResult:
    notification_ids: tuple[int, ...] = ()
    deduped_user_ids: tuple[str, ...] = ()
    deliveries: int = 0


class NotificationRouter:
    def __init__(
        self,
        state: SqliteState,
        channels: Mapping[str, Channel],
        settings: OutboxSettings | None = None,
        *,
        clock: Clock = utcnow,
        secrets: Callable[[], Iterable[str]] = tuple,
    ) -> None:
        self._state = state
        self._channels = dict(channels)
        self._settings = settings if settings is not None else OutboxSettings()
        self._clock = clock
        self._secrets = secrets
        self._prefs = PreferenceStore(state)

    @property
    def state(self) -> SqliteState:
        return self._state

    def publish(self, event: Event) -> PublishResult:
        now = self._clock()
        secrets = [s for s in self._secrets() if s]
        title = redact_text(event.title, secrets)
        body = redact_text(event.body, secrets)
        ids: list[int] = []
        deduped: list[str] = []
        deliveries = 0
        with self._state.transaction():
            for user_id in self.resolve(event.audience):
                out = self._publish_one(user_id, event, title, body, now)
                if out is None:
                    deduped.append(user_id)
                else:
                    ids.append(out[0])
                    deliveries += out[1]
        return PublishResult(tuple(ids), tuple(deduped), deliveries)

    # ---- audience -------------------------------------------------------------

    def resolve(self, audience: Audience) -> list[str]:
        """Active human users in ``audience``, sorted, without repeats."""
        active = "u.status = 'active' AND u.kind = 'human'"
        if audience.kind == "users":
            marks = ", ".join("?" for _ in audience.user_ids)
            rows = self._state.sql(
                f"SELECT u.id FROM users u WHERE u.id IN ({marks}) AND {active}",
                list(audience.user_ids),
            )
        elif audience.kind == "owner":
            rows = self._state.sql(
                "SELECT u.id FROM portfolios p JOIN users u ON u.id = p.owner_id"
                f" WHERE p.id = ? AND {active}",
                [audience.portfolio_id],
            )
        elif audience.kind == "admins":
            rows = self._state.sql(f"SELECT u.id FROM users u WHERE u.role = 'admin' AND {active}")
        else:
            modes = audience.modes or ("notify",)
            marks = ", ".join("?" for _ in modes)
            rows = self._state.sql(
                "SELECT DISTINCT s.user_id AS id FROM subscriptions s"
                " JOIN users u ON u.id = s.user_id"
                f" WHERE s.strategy_id = ? AND s.enabled = 1 AND s.mode IN ({marks})"
                f" AND {active}",
                [audience.strategy_id, *modes],
            )
        return sorted({r["id"] for r in rows})

    # ---- one user ---------------------------------------------------------------

    def _publish_one(
        self, user_id: str, event: Event, title: str, body: str, now: datetime
    ) -> tuple[int, int] | None:
        key = event.dedupe_key
        if key is not None:
            window = self._settings.dedupe_window_hours
            if window is not None:
                self._state.execute(
                    "UPDATE notification_outbox SET dedupe_active = 0"
                    " WHERE user_id = ? AND dedupe_key = ? AND dedupe_active = 1"
                    " AND created_at <= ?",
                    [user_id, key, iso(now - timedelta(hours=window))],
                )
            seen = self._state.sql(
                "SELECT 1 FROM notification_outbox"
                " WHERE user_id = ? AND dedupe_key = ? AND dedupe_active = 1",
                [user_id, key],
            )
            if seen:
                return None
        try:
            nid = self._state.execute(
                "INSERT INTO notification_outbox (user_id, category, level, urgency, title, body,"
                " deep_link, strategy_id, portfolio_id, dedupe_key, created_at)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                [
                    user_id,
                    event.category,
                    event.level,
                    event.urgency,
                    title,
                    body,
                    event.deep_link,
                    event.strategy_id,
                    event.portfolio_id,
                    key,
                    iso(now),
                ],
            ).lastrowid
        except sqlite3.IntegrityError as exc:
            # Another process enqueued the same key between our check and
            # insert. Any other integrity failure is a bug: let it raise.
            if key is None or "UNIQUE" not in str(exc):
                raise
            return None
        assert nid is not None
        context = json.dumps({"deep_link": event.deep_link, "notification_id": nid}, sort_keys=True)
        alert_id = self._state.execute(
            "INSERT INTO alerts (level, title, message, context_json, created_at, user_id,"
            " category, dedupe_key) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            [event.level, title, body, context, iso(now), user_id, event.category, key],
        ).lastrowid
        self._state.execute(
            "UPDATE notification_outbox SET alert_id = ? WHERE id = ?", [alert_id, nid]
        )
        count = 0
        for name, target_id in self._targets(user_id, event):
            self._state.execute(
                "INSERT INTO notification_deliveries (notification_id, user_id, channel,"
                " target_id, next_attempt_at, created_at, updated_at)"
                " VALUES (?, ?, ?, ?, ?, ?, ?)",
                [nid, user_id, name, target_id, iso(now), iso(now), iso(now)],
            )
            count += 1
        return nid, count

    def _targets(self, user_id: str, event: Event) -> list[tuple[str, str]]:
        out: list[tuple[str, str]] = []
        chosen: set[str] = set()
        for name, channel in sorted(self._channels.items()):
            if self._prefs.enabled(
                user_id, event.category, name, event.strategy_id, default=channel.default_enabled
            ):
                chosen.add(name)
                out += [(name, t) for t in channel.targets(self._state, user_id)]
        push = self._channels.get("webpush")
        has_push = push is not None and bool(push.targets(self._state, user_id))
        if event.urgency == "high" and not has_push:
            for name, channel in sorted(self._channels.items()):
                if not channel.fallback or name in chosen:
                    continue
                if self._prefs.explicit(user_id, event.category, name, event.strategy_id) is False:
                    continue
                out += [(name, t) for t in channel.targets(self._state, user_id)]
        return out


def configured_router(
    state: SqliteState, settings: NotifySettings | None = None
) -> NotificationRouter:
    """A router over every channel ``settings`` (default: the environment,
    :meth:`NotifySettings.from_env`) configures, so producers outside a
    request (halt trips, the quit rule, tick signals) queue real deliveries
    and not only feed rows."""
    from stonks.notify.channels import build_channels

    notify = settings if settings is not None else NotifySettings.from_env()
    return NotificationRouter(state, build_channels(notify), notify.outbox, secrets=notify.secrets)


# ---- signals (called by the signal phase, step S5) -------------------------------


@dataclass(frozen=True)
class SignalNotice:
    strategy_id: str
    ticker: str
    kind: SignalKind
    as_of: str  # ISO date of the tick
    urgency: Urgency | None = None

    def __post_init__(self) -> None:
        if self.kind not in SIGNAL_KINDS:
            raise ValueError(f"unknown signal kind {self.kind!r}")
        if not self.strategy_id or not self.ticker or not self.as_of:
            raise ValueError("a signal needs a strategy, a ticker and an as-of date")


def _signal_event(router: NotificationRouter, notice: SignalNotice) -> Event:
    rows = router.state.sql("SELECT status FROM strategies WHERE id = ?", [notice.strategy_id])
    incubating = bool(rows) and rows[0]["status"] == "shadow"
    body = f"{notice.strategy_id} on {notice.as_of}"
    if incubating:
        body += " (incubating strategy)"
    query = urlencode(
        {"strategy": notice.strategy_id, "ticker": notice.ticker, "as_of": notice.as_of}
    )
    return Event(
        category="signal",
        title=f"{notice.ticker}: {notice.kind} signal",
        body=body,
        audience=Audience.subscribers(notice.strategy_id),
        urgency=notice.urgency,
        dedupe_key=(f"signal:{notice.strategy_id}:{notice.ticker}:{notice.kind}:{notice.as_of}"),
        deep_link=f"/signals?{query}",
        strategy_id=notice.strategy_id,
    )


def notify_signal(
    router: NotificationRouter,
    *,
    strategy_id: str,
    ticker: str,
    kind: SignalKind,
    as_of: str,
    urgency: Urgency | None = None,
) -> PublishResult:
    """Tell a strategy's notify-mode subscribers about one signal event.

    Idempotent per ``(strategy, ticker, kind, as_of)``: a re-run tick sends
    nothing twice. The payload names the ticker, the kind and the strategy,
    never sizes or prices.
    """
    notice = SignalNotice(strategy_id, ticker, kind, as_of, urgency)
    return router.publish(_signal_event(router, notice))


def notify_signals(
    router: NotificationRouter, notices: Iterable[SignalNotice]
) -> list[PublishResult]:
    """Many signal events in one transaction (one tick's signal phase)."""
    with router.state.transaction():
        return [router.publish(_signal_event(router, n)) for n in notices]

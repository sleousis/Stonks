"""DeliveryWorker: sends what the router queued, outside any producer's
transaction, so a push-service outage never blocks a tick.

One pass (:meth:`DeliveryWorker.run_once`):

1. **Claim** due rows (``pending``, ``deferred``, ``failed``, or ``sending``
   whose lease ran out, i.e. a worker that crashed mid-send) in one UPDATE,
   setting ``sending`` with a lease. Two workers never claim the same row.
   Delivery is at-least-once: a crash after a send but before the row is
   marked sends it again, and the push ``Topic`` collapses such repeats.
2. **Quiet hours.** ``low``/``normal`` rows for a user inside their quiet
   hours go back to ``deferred`` until the window ends (in the user's time
   zone). ``high`` always goes.
3. **Digest.** Rows held by quiet hours that come due together for the same
   user, channel and target go out as one "N notifications" message.
4. **Send** and record: ``sent``; ``retry`` becomes ``failed`` with
   exponential backoff (``Retry-After`` honoured) until ``max_attempts``,
   then ``dead``; ``dead`` and ``gone`` are dead at once (the channel hook
   revokes a gone push subscription).

A worker holds one ``SqliteState`` connection, so build it on the thread
that runs it (SQLite connections are per thread).
"""

from __future__ import annotations

import threading
from collections import defaultdict
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

from stonks.logging import get_logger
from stonks.notify.channels import Channel, DeliveryResult
from stonks.notify.events import TTL_SECONDS, Message
from stonks.notify.prefs import PreferenceStore
from stonks.notify.router import Clock, iso, utcnow
from stonks.notify.settings import OutboxSettings
from stonks.notify.store import redact_text
from stonks.store.state import SqliteState

_log = get_logger("stonks.notify.worker")

_ERROR_MAX = 500


@dataclass
class WorkerStats:
    sent: int = 0
    retried: int = 0
    dead: int = 0
    deferred: int = 0
    skipped: int = 0

    @property
    def total(self) -> int:
        return self.sent + self.retried + self.dead + self.deferred + self.skipped


class DeliveryWorker:
    def __init__(
        self,
        state: SqliteState,
        channels: Mapping[str, Channel],
        settings: OutboxSettings | None = None,
        *,
        clock: Clock = utcnow,
    ) -> None:
        self._state = state
        self._channels = dict(channels)
        self._settings = settings if settings is not None else OutboxSettings()
        self._clock = clock
        self._prefs = PreferenceStore(state)

    def run_forever(self, stop: threading.Event, interval_seconds: float = 5.0) -> None:
        """Deliver until ``stop`` is set; sleeps only when a pass found nothing."""
        while not stop.is_set():
            try:
                busy = self.run_once().total > 0
            except Exception as exc:  # keep the loop alive; the next pass retries
                _log.error("notify.worker.pass_failed", error_type=type(exc).__name__)
                busy = False
            if not busy:
                stop.wait(interval_seconds)

    def run_once(self) -> WorkerStats:
        now = self._clock()
        stats = WorkerStats()
        rows = self._claim(now)
        if not rows:
            return stats
        notes = self._notifications({r["notification_id"] for r in rows})
        quiet_cache: dict[str, Any] = {}
        digests: dict[tuple[str, str, str], list[dict]] = defaultdict(list)
        singles: list[dict] = []
        for row in rows:
            note = notes.get(row["notification_id"])
            if note is None:  # the notification was deleted with its user
                self._finish(row, "skipped", now, error="notification removed")
                stats.skipped += 1
                continue
            if note["urgency"] != "high":
                end = self._quiet_end(row["user_id"], now, quiet_cache)
                if end is not None:
                    self._state.execute(
                        "UPDATE notification_deliveries SET status = 'deferred', deferred = 1,"
                        " next_attempt_at = ?, updated_at = ? WHERE id = ?",
                        [iso(end), iso(now), row["id"]],
                    )
                    stats.deferred += 1
                    continue
            if row["deferred"]:
                digests[(row["user_id"], row["channel"], row["target_id"])].append(row)
            else:
                singles.append(row)
        for row in singles:
            self._deliver([row], self._message(notes[row["notification_id"]]), now, stats)
        for (user_id, _, _), group in digests.items():
            if len(group) == 1:
                message = self._message(notes[group[0]["notification_id"]])
            else:
                ttl = max(TTL_SECONDS[notes[r["notification_id"]]["category"]] for r in group)
                message = Message.digest(user_id, len(group), ttl)
            self._deliver(group, message, now, stats)
        return stats

    # ---- claiming ---------------------------------------------------------------

    def _claim(self, now: datetime) -> list[dict]:
        lease = now + timedelta(seconds=self._settings.lease_seconds)
        cur = self._state.execute(
            "UPDATE notification_deliveries SET status = 'sending', next_attempt_at = ?,"
            " updated_at = ?"
            " WHERE id IN (SELECT id FROM notification_deliveries"
            "   WHERE status IN ('pending', 'deferred', 'failed', 'sending')"
            "   AND next_attempt_at <= ? ORDER BY next_attempt_at, id LIMIT ?)"
            " RETURNING id, notification_id, user_id, channel, target_id, attempts, deferred",
            [iso(lease), iso(now), iso(now), self._settings.batch_size],
        )
        rows = [dict(r) for r in cur.fetchall()]
        return sorted(rows, key=lambda r: r["id"])

    def _notifications(self, ids: set[int]) -> dict[int, dict]:
        if not ids:
            return {}
        marks = ", ".join("?" for _ in ids)
        rows = self._state.sql(
            f"SELECT * FROM notification_outbox WHERE id IN ({marks})", sorted(ids)
        )
        return {r["id"]: dict(r) for r in rows}

    def _quiet_end(self, user_id: str, now: datetime, cache: dict[str, Any]) -> datetime | None:
        if user_id not in cache:
            try:
                cache[user_id] = self._prefs.settings(user_id).quiet_hours
            except ValueError:  # a malformed stored time: treat as no quiet hours
                cache[user_id] = None
        quiet = cache[user_id]
        if quiet is None or not quiet.is_quiet(now):
            return None
        return quiet.ends_after(now)

    @staticmethod
    def _message(note: dict) -> Message:
        return Message(
            notification_id=note["id"],
            user_id=note["user_id"],
            category=note["category"],
            level=note["level"],
            urgency=note["urgency"],
            title=note["title"],
            body=note["body"],
            deep_link=note["deep_link"],
            dedupe_key=note["dedupe_key"],
            ttl_seconds=TTL_SECONDS[note["category"]],
        )

    # ---- sending ----------------------------------------------------------------

    def _deliver(
        self, rows: list[dict], message: Message, now: datetime, stats: WorkerStats
    ) -> None:
        head = rows[0]
        user_id, name, target_id = head["user_id"], head["channel"], head["target_id"]
        channel = self._channels.get(name)
        if channel is None:
            result = DeliveryResult.retry(f"channel {name!r} is not configured")
        else:
            target = channel.resolve(self._state, user_id, target_id)
            if target is None:
                for row in rows:
                    self._finish(row, "skipped", now, error="target removed")
                stats.skipped += len(rows)
                return
            try:
                result = channel.send(message, target)
            except Exception as exc:  # a channel bug must not stop the pass
                result = DeliveryResult.retry(channel.redact(f"{type(exc).__name__}: {exc}"))
        error = redact_text(result.error or "")[:_ERROR_MAX] or None
        dead_before = stats.dead
        if result.outcome == "sent":
            for row in rows:
                self._finish(row, "sent", now, attempts=row["attempts"] + 1)
            stats.sent += len(rows)
            if channel is not None:
                channel.on_sent(self._state, user_id, target_id, now)
            return
        if result.outcome == "retry":
            for row in rows:
                attempts = row["attempts"] + 1
                if attempts >= self._settings.max_attempts:
                    self._finish(row, "dead", now, attempts=attempts, error=error)
                    stats.dead += 1
                else:
                    delay = self._backoff(attempts, result.retry_after)
                    self._finish(
                        row, "failed", now, attempts=attempts, error=error, next_at=now + delay
                    )
                    stats.retried += 1
        else:  # dead or gone
            for row in rows:
                self._finish(row, "dead", now, attempts=row["attempts"] + 1, error=error)
            stats.dead += len(rows)
        if channel is not None:
            channel.on_failed(self._state, user_id, target_id, result, now)
        if stats.dead > dead_before:
            _log.warning(
                "notify.delivery.failed",
                channel=name,
                user_id=user_id,
                deliveries=[r["id"] for r in rows],
                outcome=result.outcome,
                error=error,
            )

    def _backoff(self, attempts: int, retry_after: float | None) -> timedelta:
        s = self._settings
        delay = min(s.backoff_base_seconds * 2 ** (attempts - 1), s.backoff_max_seconds)
        if retry_after is not None:
            delay = max(delay, retry_after)
        return timedelta(seconds=delay)

    def _finish(
        self,
        row: dict,
        status: str,
        now: datetime,
        *,
        attempts: int | None = None,
        error: str | None = None,
        next_at: datetime | None = None,
    ) -> None:
        self._state.execute(
            "UPDATE notification_deliveries SET status = ?, attempts = ?, last_error = ?,"
            " next_attempt_at = ?, sent_at = ?, updated_at = ? WHERE id = ?",
            [
                status,
                row["attempts"] if attempts is None else attempts,
                error,
                iso(next_at or now),
                iso(now) if status == "sent" else None,
                iso(now),
                row["id"],
            ],
        )

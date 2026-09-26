"""Channel fakes for router and worker tests: no network, scripted outcomes."""

from __future__ import annotations

from collections import deque
from typing import Any

from stonks.notify.channels import Channel, DeliveryResult
from stonks.notify.events import Message
from stonks.store.state import SqliteState


class FakeChannel(Channel):
    """A single-target channel that records sends and replays outcomes."""

    def __init__(
        self,
        name: str,
        *,
        default_enabled: bool = False,
        fallback: bool = False,
        reachable: set[str] | None = None,
    ) -> None:
        self.name = name  # type: ignore[misc]
        self.default_enabled = default_enabled  # type: ignore[misc]
        self.fallback = fallback  # type: ignore[misc]
        self.reachable = reachable  # None = everyone
        self.sent: list[tuple[Message, Any]] = []
        self.outcomes: deque[DeliveryResult | Exception] = deque()
        self.failed: list[tuple[str, str, str]] = []
        self.ok: list[tuple[str, str]] = []

    def resolve(self, state: SqliteState, user_id: str, target_id: str) -> Any | None:
        if self.reachable is not None and user_id not in self.reachable:
            return None
        return f"{self.name}:{user_id}"

    def send(self, message: Message, target: Any) -> DeliveryResult:
        self.sent.append((message, target))
        if self.outcomes:
            outcome = self.outcomes.popleft()
            if isinstance(outcome, Exception):
                raise outcome
            return outcome
        return DeliveryResult.sent()

    def on_sent(self, state, user_id, target_id, now) -> None:
        self.ok.append((user_id, target_id))

    def on_failed(self, state, user_id, target_id, result, now) -> None:
        self.failed.append((user_id, target_id, result.outcome))


class FakePush(FakeChannel):
    """Multi-target like Web Push: one target per push_subscriptions row."""

    def __init__(self) -> None:
        super().__init__("webpush", default_enabled=True)

    def targets(self, state: SqliteState, user_id: str) -> list[str]:
        rows = state.sql(
            "SELECT id FROM push_subscriptions WHERE user_id = ? AND revoked_at IS NULL"
            " ORDER BY id",
            [user_id],
        )
        return [r["id"] for r in rows]

    def resolve(self, state: SqliteState, user_id: str, target_id: str) -> Any | None:
        rows = state.sql(
            "SELECT id FROM push_subscriptions WHERE id = ? AND user_id = ? AND revoked_at IS NULL",
            [target_id, user_id],
        )
        return rows[0]["id"] if rows else None


def add_device(state: SqliteState, user_id: str, device_id: str) -> None:
    state.execute(
        "INSERT INTO push_subscriptions (id, user_id, endpoint, p256dh, auth, created_at)"
        " VALUES (?, ?, ?, 'k', 'a', '2026-01-01T00:00:00+00:00')",
        [device_id, user_id, f"https://push.example/{device_id}"],
    )

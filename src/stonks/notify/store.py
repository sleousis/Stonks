"""Notifier that persists every alert to the state DB's ``alerts`` table.

The table backs the trader console's alerts feed (``GET /api/alerts``), so
it keeps every level: :class:`CompositeNotifier`'s ``min_level`` governs
delivery (log, webhook), not the record. Everything written is redacted
first: configured secret values and ``key=value`` credential pairs are
scrubbed from the title, message and every string in the context, and
context entries whose key names a credential are replaced outright.
"""

from __future__ import annotations

import json
import re
from collections.abc import Callable, Iterable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from stonks.ingest.redact import REDACTED, redact_secrets
from stonks.notify.base import Notification, Notifier
from stonks.store.state import SqliteState

#: Context keys whose values are credentials whatever they look like.
_SECRET_KEY_RE = re.compile(
    r"(?i)(token|secret|password|passwd|api[_-]?key|authorization|credential|cookie|webhook)"
)
_BEARER_RE = re.compile(r"(?i)\b(bearer|basic)\s+[A-Za-z0-9._~+/=-]+")


class StoreNotifier(Notifier):
    receives_all_levels = True

    def __init__(
        self,
        state_path: str | Path,
        secrets: Callable[[], Iterable[str]] = tuple,
    ) -> None:
        self._path = Path(state_path)
        self._secrets = secrets

    def __repr__(self) -> str:
        return f"StoreNotifier(state_path={self._path.as_posix()!r})"

    def _send(self, notification: Notification) -> None:
        secrets = [s for s in self._secrets() if s]
        context = _redact_value(notification.json_fields(), secrets)
        with SqliteState(self._path) as state:
            state.execute(
                "INSERT INTO alerts (level, title, message, context_json, created_at) "
                "VALUES (?, ?, ?, ?, ?)",
                [
                    notification.level,
                    redact_text(notification.title, secrets),
                    redact_text(notification.message, secrets),
                    json.dumps(context, sort_keys=True),
                    datetime.now(UTC).isoformat(timespec="seconds"),
                ],
            )

    def _redact(self, text: str) -> str:
        return redact_text(text, [s for s in self._secrets() if s])


def redact_text(text: str, secrets: Iterable[str] = ()) -> str:
    """Scrub secret values, credential query params and auth-header values."""
    text = redact_secrets(text, secrets)
    return _BEARER_RE.sub(lambda m: f"{m.group(1)} {REDACTED}", text)


def _redact_value(value: Any, secrets: list[str]) -> Any:
    if isinstance(value, dict):
        return {
            k: REDACTED if _SECRET_KEY_RE.search(str(k)) else _redact_value(v, secrets)
            for k, v in value.items()
        }
    if isinstance(value, list):
        return [_redact_value(v, secrets) for v in value]
    if isinstance(value, str):
        return redact_text(value, secrets)
    return value

"""Web Push (RFC 8030/8291/8292) with VAPID, wrapping ``pywebpush``.

``pywebpush`` and ``py_vapid`` are used only in this module; nothing else in
Stonks sees their types.

- **Keys.** One VAPID pair (ECDSA P-256) per deployment, from
  ``python -m stonks.notify vapid-keygen``. The public key goes to the
  browser (``applicationServerKey``); the private key stays in the
  environment (``STONKS_VAPID_PRIVATE_KEY``) and is never logged.
- **Subscriptions.** One ``push_subscriptions`` row per browser or installed
  app. The endpoint is a capability URL (anyone holding it plus the keys can
  push to the device), so it's never logged or returned in full.
- **Responses.** 404/410: the subscription is gone, revoke it. 429 and 5xx:
  retry (honouring ``Retry-After``). Other 4xx: this message is dead. After
  ``push_failure_limit`` consecutive failures the subscription is disabled
  and the user is told in-app.
- **Payload.** Angular's service worker shape (``{"notification": {...}}``,
  ``onActionClick`` opens the deep link), minimal (title, one line, link),
  and at most :data:`MAX_PAYLOAD_BYTES` before encryption (push services
  cap the encrypted record at 4096 bytes).
- **Headers.** ``TTL`` per category, ``Urgency`` (``high`` for risk), and
  ``Topic`` (a hash of the dedupe key) so a newer message replaces an
  undelivered older one.
- **iOS.** Safari delivers Web Push only to PWAs added to the home screen
  (iOS/iPadOS 16.4+), through ``web.push.apple.com``, which also requires a
  real ``mailto:`` or ``https:`` VAPID subject. The UI explains this on
  iPhone; nothing differs server-side.
"""

from __future__ import annotations

import base64
import json
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any
from urllib.parse import urlsplit

import requests
from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat
from py_vapid import Vapid02
from pywebpush import WebPushException, webpush

from stonks.ingest.redact import REDACTED
from stonks.logging import get_logger
from stonks.notify.base import redact_url
from stonks.notify.channels import Channel, DeliveryResult, register_channel
from stonks.notify.events import Message
from stonks.notify.settings import NotifySettings, WebPushSettings
from stonks.store.state import SqliteState

_log = get_logger("stonks.notify.webpush")

#: Plaintext cap; the aes128gcm record adds ~100 bytes of overhead and push
#: services refuse records over 4096 bytes.
MAX_PAYLOAD_BYTES = 3072

#: The console's PWA icon (web/public/icons), served from the app origin.
ICON = "/icons/icon-192.png"

_GONE = (404, 410)
_RETRY_AFTER_MAX = 3600.0


def _b64url(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def _b64url_decode(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


@dataclass(frozen=True)
class VapidKeyPair:
    public_key: str  # base64url uncompressed point: the browser's applicationServerKey
    private_key: str = field(repr=False)  # base64url raw scalar: a secret


def generate_vapid_keys() -> VapidKeyPair:
    vapid = Vapid02()
    vapid.generate_keys()
    return VapidKeyPair(
        public_key=_public_key_of(vapid),
        private_key=_b64url(vapid.private_key.private_numbers().private_value.to_bytes(32, "big")),
    )


def _public_key_of(vapid: Vapid02) -> str:
    return _b64url(vapid.public_key.public_bytes(Encoding.X962, PublicFormat.UncompressedPoint))


@dataclass(frozen=True)
class PushTarget:
    endpoint: str = field(repr=False)
    p256dh: str = field(repr=False)
    auth: str = field(repr=False)
    subscription_id: str = ""

    def info(self) -> dict[str, Any]:
        return {"endpoint": self.endpoint, "keys": {"p256dh": self.p256dh, "auth": self.auth}}


def encode_payload(message: Message, limit: int = MAX_PAYLOAD_BYTES) -> bytes:
    """The JSON the service worker receives, shrunk to fit ``limit``."""
    link = message.deep_link or "/"
    title, body = message.title, message.body
    for _ in range(3):
        data = {
            "notification": {
                "title": title,
                "body": body,
                "icon": ICON,
                "tag": message.topic or f"n{message.notification_id or 0}",
                "requireInteraction": message.urgency == "high",
                "data": {
                    "url": link,
                    "category": message.category,
                    "onActionClick": {
                        "default": {"operation": "navigateLastFocusedOrOpen", "url": link}
                    },
                },
            }
        }
        raw = json.dumps(data, separators=(",", ":"), ensure_ascii=False).encode()
        if len(raw) <= limit:
            return raw
        # Shed the optional parts first: the link, then the body.
        link, body = "/", body[:40]
        title = title[:40]
    return json.dumps({"notification": {"title": title[:20]}}).encode()


def _retry_after(response: Any) -> float | None:
    headers = getattr(response, "headers", None) or {}
    value = headers.get("Retry-After") or headers.get("retry-after")
    try:
        return min(float(value), _RETRY_AFTER_MAX) if value is not None else None
    except (TypeError, ValueError):
        return None


@register_channel("webpush")
class WebPushChannel(Channel):
    default_enabled = True

    def __init__(
        self,
        settings: WebPushSettings,
        *,
        failure_limit: int = 5,
        session: Any | None = None,
        sender: Callable[..., Any] = webpush,
    ) -> None:
        if not settings.configured or settings.private_key is None:
            raise ValueError("Web Push needs STONKS_VAPID_PUBLIC_KEY, _PRIVATE_KEY and _SUBJECT")
        self._private = settings.private_key.get_secret_value()
        self._vapid = Vapid02.from_string(self._private)
        self._subject = str(settings.subject)
        self._timeout = settings.timeout_seconds
        self._failure_limit = failure_limit
        self._session = session if session is not None else requests.Session()
        self._sender = sender
        self.public_key = settings.public_key

    def __repr__(self) -> str:
        return f"WebPushChannel(subject={self._subject!r})"

    @classmethod
    def from_settings(cls, settings: NotifySettings) -> WebPushChannel | None:
        wp = settings.webpush
        if not wp.configured:
            return None
        if not str(wp.subject).startswith(("mailto:", "https://")):
            _log.error("notify.webpush.bad_subject", hint="use mailto:you@example.com or https://")
            return None
        try:
            channel = cls(wp, failure_limit=settings.outbox.push_failure_limit)
        except Exception as exc:  # the key's text never reaches the log
            _log.error("notify.webpush.bad_private_key", error_type=type(exc).__name__)
            return None
        if _public_key_of(channel._vapid) != wp.public_key:
            _log.error("notify.webpush.key_mismatch", hint="public key isn't the private key's")
            return None
        return channel

    # ---- targets -------------------------------------------------------------

    def targets(self, state: SqliteState, user_id: str) -> list[str]:
        rows = state.sql(
            "SELECT id FROM push_subscriptions WHERE user_id = ? AND revoked_at IS NULL"
            " ORDER BY created_at, id",
            [user_id],
        )
        return [r["id"] for r in rows]

    def resolve(self, state: SqliteState, user_id: str, target_id: str) -> PushTarget | None:
        rows = state.sql(
            "SELECT endpoint, p256dh, auth FROM push_subscriptions"
            " WHERE id = ? AND user_id = ? AND revoked_at IS NULL",
            [target_id, user_id],
        )
        if not rows:
            return None
        r = rows[0]
        return PushTarget(r["endpoint"], r["p256dh"], r["auth"], subscription_id=target_id)

    # ---- sending -------------------------------------------------------------

    def send(self, message: Message, target: PushTarget) -> DeliveryResult:
        headers = {"Urgency": message.urgency}
        if message.topic:
            headers["Topic"] = message.topic
        try:
            self._sender(
                subscription_info=target.info(),
                data=encode_payload(message),
                vapid_private_key=self._vapid,
                vapid_claims={"sub": self._subject},
                ttl=message.ttl_seconds,
                headers=headers,
                timeout=self._timeout,
                requests_session=self._session,
            )
        except WebPushException as exc:
            response = exc.response
            status = getattr(response, "status_code", None)
            error = self.redact(f"push service answered {status}: {exc}", target)
            if status in _GONE:
                return DeliveryResult.gone(error)
            if status == 429 or status is None or status >= 500:
                return DeliveryResult.retry(error, after=_retry_after(response))
            return DeliveryResult.dead(error)
        except Exception as exc:
            return DeliveryResult.retry(self.redact(f"{type(exc).__name__}: {exc}", target))
        return DeliveryResult.sent()

    def redact(self, text: str, target: PushTarget | None = None) -> str:
        replacements = {self._private: REDACTED}
        if target is not None:
            parts = urlsplit(target.endpoint)
            replacements[target.endpoint] = redact_url(target.endpoint)
            replacements[target.p256dh] = REDACTED
            replacements[target.auth] = REDACTED
            if parts.path not in ("", "/"):
                replacements[parts.path] = "/***"
        # Longest first, so the full endpoint wins over its own path.
        for secret in sorted(filter(None, replacements), key=len, reverse=True):
            text = text.replace(secret, replacements[secret])
        return text

    # ---- bookkeeping ---------------------------------------------------------

    def on_sent(self, state: SqliteState, user_id: str, target_id: str, now: datetime) -> None:
        state.execute(
            "UPDATE push_subscriptions SET last_success_at = ?, failure_count = 0 WHERE id = ?",
            [now.isoformat(timespec="seconds"), target_id],
        )

    def on_failed(
        self,
        state: SqliteState,
        user_id: str,
        target_id: str,
        result: DeliveryResult,
        now: datetime,
    ) -> None:
        ts = now.isoformat(timespec="seconds")
        if result.outcome == "gone":
            state.execute(
                "UPDATE push_subscriptions SET revoked_at = ?, revoked_reason = 'gone'"
                " WHERE id = ? AND revoked_at IS NULL",
                [ts, target_id],
            )
            _log.info("notify.webpush.revoked", user_id=user_id, subscription_id=target_id)
            return
        state.execute(
            "UPDATE push_subscriptions SET failure_count = failure_count + 1 WHERE id = ?",
            [target_id],
        )
        disabled = state.execute(
            "UPDATE push_subscriptions SET revoked_at = ?, revoked_reason = 'failures'"
            " WHERE id = ? AND revoked_at IS NULL AND failure_count >= ?",
            [ts, target_id, self._failure_limit],
        ).rowcount
        if disabled:
            _log.warning("notify.webpush.disabled", user_id=user_id, subscription_id=target_id)
            state.execute(
                "INSERT INTO alerts (level, title, message, context_json, created_at, user_id,"
                " category) VALUES ('warning', ?, ?, '{}', ?, ?, 'system')",
                [
                    "Push notifications stopped on a device",
                    f"{self._failure_limit} deliveries in a row failed, so this device was"
                    " turned off. Turn notifications on again in Settings.",
                    ts,
                    user_id,
                ],
            )


def vapid_public_key(settings: NotifySettings) -> str | None:
    """The key the browser subscribes with (``GET /api/push/vapid-key``), or
    None when Web Push can't send (so the console doesn't subscribe)."""
    channel = WebPushChannel.from_settings(settings)
    return channel.public_key if channel is not None else None

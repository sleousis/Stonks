"""Web Push channel: VAPID keys, payload shape and size, headers, and how
push-service responses map to sent / retry / dead / gone. Hermetic: the
real pywebpush encrypts and signs, but the HTTP session is a fake."""

from __future__ import annotations

import base64
import json

import pytest
import requests
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat

from stonks.notify.events import Message
from stonks.notify.settings import NotifySettings, WebPushSettings
from stonks.notify.webpush import (
    MAX_PAYLOAD_BYTES,
    PushTarget,
    WebPushChannel,
    encode_payload,
    generate_vapid_keys,
    vapid_public_key,
)

ENDPOINT = "https://fcm.googleapis.com/fcm/send/DEVICE-SECRET-TOKEN"


def _b64(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


@pytest.fixture(scope="module")
def keys():
    return generate_vapid_keys()


@pytest.fixture
def settings(keys) -> WebPushSettings:
    return WebPushSettings(
        public_key=keys.public_key,
        private_key=keys.private_key,
        subject="mailto:ops@example.com",
    )


def _target() -> PushTarget:
    client = ec.generate_private_key(ec.SECP256R1())
    p256dh = client.public_key().public_bytes(Encoding.X962, PublicFormat.UncompressedPoint)
    return PushTarget(endpoint=ENDPOINT, p256dh=_b64(p256dh), auth=_b64(b"0123456789abcdef"))


def _message(**kw) -> Message:
    base = {
        "notification_id": 7,
        "user_id": "usr_a",
        "category": "risk",
        "level": "error",
        "urgency": "high",
        "title": "Trading halted",
        "body": "Drawdown limit hit",
        "deep_link": "/risk",
        "dedupe_key": "risk:halt:pf_1:2026-01-05",
        "ttl_seconds": 86400,
    }
    base.update(kw)
    return Message(**base)


class _Resp:
    def __init__(self, status: int, headers: dict | None = None, text: str = "") -> None:
        self.status_code = status
        self.reason = "X"
        self.headers = headers or {}
        self.text = text


class _Session:
    def __init__(self, resp: _Resp | None = None, exc: Exception | None = None) -> None:
        self.resp = resp or _Resp(201)
        self.exc = exc
        self.calls: list[dict] = []

    def post(self, url, timeout=None, data=None, headers=None):
        self.calls.append({"url": url, "data": data, "headers": headers, "timeout": timeout})
        if self.exc:
            raise self.exc
        return self.resp


def test_generated_keys_are_p256_base64url(keys):
    pub = base64.urlsafe_b64decode(keys.public_key + "==")
    priv = base64.urlsafe_b64decode(keys.private_key + "==")
    assert len(pub) == 65 and pub[0] == 4
    assert len(priv) == 32
    assert "SECRET" not in repr(keys) and keys.private_key not in repr(keys)


def test_from_settings_needs_keys_and_a_valid_subject(settings, keys):
    assert WebPushChannel.from_settings(NotifySettings(webpush=settings)) is not None
    assert WebPushChannel.from_settings(NotifySettings()) is None
    bad_subject = settings.model_copy(update={"subject": "ops@example.com"})
    assert WebPushChannel.from_settings(NotifySettings(webpush=bad_subject)) is None


def test_from_settings_refuses_a_mismatched_pair(settings):
    other = generate_vapid_keys()
    mixed = settings.model_copy(update={"public_key": other.public_key})
    assert WebPushChannel.from_settings(NotifySettings(webpush=mixed)) is None


def test_from_settings_refuses_a_garbage_private_key(settings, capsys):
    bad = settings.model_copy(update={"private_key": "not-a-key-SECRET"})
    assert WebPushChannel.from_settings(NotifySettings(webpush=bad)) is None
    assert "not-a-key-SECRET" not in capsys.readouterr().err


def test_payload_uses_the_angular_service_worker_shape():
    payload = json.loads(encode_payload(_message()))
    n = payload["notification"]
    assert n["title"] == "Trading halted"
    assert n["body"] == "Drawdown limit hit"
    assert n["data"]["url"] == "/risk"
    assert n["data"]["onActionClick"]["default"] == {
        "operation": "navigateLastFocusedOrOpen",
        "url": "/risk",
    }
    assert n["tag"] == _message().topic
    assert n["requireInteraction"] is True  # high urgency


def test_payload_never_exceeds_the_limit():
    big = _message(title="t" * 80, body="b" * 240, deep_link="/" + "x" * 500)
    assert len(encode_payload(big)) <= MAX_PAYLOAD_BYTES
    small = encode_payload(big, limit=400)
    assert len(small) <= 400
    assert json.loads(small)["notification"]["title"]


def test_send_encrypts_signs_and_sets_headers(settings):
    session = _Session()
    channel = WebPushChannel(settings, session=session)
    result = channel.send(_message(), _target())
    assert result.outcome == "sent"
    [call] = session.calls
    assert call["url"] == ENDPOINT
    h = {k.lower(): v for k, v in call["headers"].items()}
    assert h["ttl"] == "86400"
    assert h["urgency"] == "high"
    assert h["topic"] == _message().topic
    assert h["content-encoding"] == "aes128gcm"
    assert h["authorization"].startswith("vapid t=")
    assert b"Trading halted" not in call["data"]  # encrypted


def test_low_urgency_and_no_topic(settings):
    session = _Session()
    WebPushChannel(settings, session=session).send(
        _message(urgency="low", dedupe_key=None), _target()
    )
    h = {k.lower(): v for k, v in session.calls[0]["headers"].items()}
    assert h["urgency"] == "low"
    assert "topic" not in h


@pytest.mark.parametrize(
    ("status", "outcome"),
    [
        (404, "gone"),
        (410, "gone"),
        (429, "retry"),
        (500, "retry"),
        (503, "retry"),
        (400, "dead"),
        (403, "dead"),
        (413, "dead"),
    ],
)
def test_push_service_statuses(settings, status, outcome):
    resp = _Resp(status, text=f"error for {ENDPOINT}")
    result = WebPushChannel(settings, session=_Session(resp)).send(_message(), _target())
    assert result.outcome == outcome
    assert "DEVICE-SECRET-TOKEN" not in (result.error or "")


def test_retry_after_is_honoured(settings):
    resp = _Resp(429, headers={"Retry-After": "120"})
    result = WebPushChannel(settings, session=_Session(resp)).send(_message(), _target())
    assert result.retry_after == 120


def test_network_errors_retry_and_are_redacted(settings):
    exc = requests.ConnectionError(f"Max retries exceeded with url: {ENDPOINT}")
    result = WebPushChannel(settings, session=_Session(exc=exc)).send(_message(), _target())
    assert result.outcome == "retry"
    assert "DEVICE-SECRET-TOKEN" not in result.error


def test_redact_scrubs_endpoint_keys_and_private_key(settings, keys):
    channel = WebPushChannel(settings)
    text = f"{ENDPOINT} /fcm/send/DEVICE-SECRET-TOKEN {keys.private_key}"
    clean = channel.redact(text, _target())
    assert "DEVICE-SECRET-TOKEN" not in clean
    assert keys.private_key not in clean
    assert "fcm.googleapis.com" in clean  # host is fine, it's not secret


def test_payload_carries_the_app_icon():
    n = json.loads(encode_payload(_message()))["notification"]
    assert n["icon"] == "/icons/icon-192.png"


def test_vapid_public_key_only_when_push_works(settings, keys):
    assert vapid_public_key(NotifySettings(webpush=settings)) == keys.public_key
    assert vapid_public_key(NotifySettings()) is None
    other = generate_vapid_keys()
    mixed = settings.model_copy(update={"public_key": other.public_key})
    assert vapid_public_key(NotifySettings(webpush=mixed)) is None

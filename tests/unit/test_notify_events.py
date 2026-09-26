"""Events, the minimal channel message, the channel registry and settings."""

from __future__ import annotations

import re

import pytest
import requests

from stonks.notify.channels import (
    Channel,
    DeliveryResult,
    LogChannel,
    WebhookChannel,
    build_channels,
    channel_names,
    register_channel,
)
from stonks.notify.events import (
    BODY_MAX,
    TITLE_MAX,
    Audience,
    Event,
    Message,
    push_topic,
)
from stonks.notify.settings import NotifySettings


def _event(**kw) -> Event:
    base = {
        "category": "signal",
        "title": "AAPL.US: entry",
        "body": "momentum",
        "audience": Audience.admins(),
    }
    base.update(kw)
    return Event(**base)


def test_urgency_defaults_follow_category_and_level():
    assert _event().urgency == "normal"
    assert _event(category="risk").urgency == "high"
    assert _event(level="error").urgency == "high"
    assert _event(category="risk", urgency="low").urgency == "low"


def test_ttl_by_category():
    assert _event().ttl_seconds == 12 * 3600
    assert _event(category="risk").ttl_seconds == 24 * 3600


def test_text_is_single_line_and_capped():
    e = _event(title="a\nb\r\tc" + "x" * 500, body="line1\nline2" + "y" * 1000)
    assert "\n" not in e.title and "\r" not in e.title
    assert len(e.title) <= TITLE_MAX
    assert len(e.body) <= BODY_MAX
    assert e.title.startswith("a b c")


@pytest.mark.parametrize(
    "link",
    [
        "https://evil.example/phish",
        "//evil.example/x",
        "/\\evil.example",
        "javascript:alert(1)",
        "signals",
        "/" + "a" * 600,
    ],
)
def test_deep_link_must_be_app_relative(link):
    with pytest.raises(ValueError):
        _event(deep_link=link)


def test_good_deep_link_is_kept():
    assert _event(deep_link="/signals?strategy=s1").deep_link == "/signals?strategy=s1"


def test_empty_title_rejected():
    with pytest.raises(ValueError):
        _event(title="   ")


def test_unknown_category_rejected():
    with pytest.raises(ValueError):
        _event(category="gossip")


def test_audiences():
    assert Audience.users("u1", "u2").user_ids == ("u1", "u2")
    assert Audience.owner_of("pf_1").portfolio_id == "pf_1"
    sub = Audience.subscribers("s1")
    assert sub.strategy_id == "s1" and sub.modes == ("notify",)
    with pytest.raises(ValueError):
        Audience.users()


def test_push_topic_is_short_urlsafe_and_opaque():
    topic = push_topic("signal:momentum:AAPL.US:entry:2026-01-05")
    assert re.fullmatch(r"[A-Za-z0-9_-]{1,32}", topic)
    assert "AAPL" not in topic
    assert push_topic("x") != push_topic("y")
    assert push_topic(None) is None


def _message(**kw) -> Message:
    base = {
        "notification_id": 1,
        "user_id": "usr_a",
        "category": "order",
        "level": "warning",
        "urgency": "normal",
        "title": "Order rejected",
        "body": "1 order rejected",
        "deep_link": "/orders",
        "dedupe_key": None,
        "ttl_seconds": 3600,
    }
    base.update(kw)
    return Message(**base)


def test_builtin_channels_are_registered():
    assert {"log", "webhook", "webpush", "email"} <= set(channel_names())


def test_build_channels_skips_unconfigured(monkeypatch):
    channels = build_channels(NotifySettings.from_env())
    assert "log" in channels and "webhook" in channels
    assert "webpush" not in channels  # no VAPID keys
    assert "email" not in channels  # no SMTP host


def test_register_channel_rejects_duplicates():
    with pytest.raises(ValueError):

        @register_channel("log")
        class Again(LogChannel):
            pass


def test_register_channel_requires_a_channel():
    with pytest.raises(TypeError):
        register_channel("nope")(object)  # type: ignore[arg-type]


def test_log_channel_sends(capsys):
    assert LogChannel().send(_message(), "").outcome == "sent"


class _Resp:
    def __init__(self, status: int) -> None:
        self.status_code = status

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            err = requests.HTTPError(
                f"{self.status_code} Client Error for url: https://hooks.example/T0/SECRET"
            )
            err.response = self  # type: ignore[attr-defined]
            raise err


class _Session:
    def __init__(self, status: int = 200, exc: Exception | None = None) -> None:
        self.status = status
        self.exc = exc
        self.calls: list[dict] = []

    def post(self, url, json=None, timeout=None, headers=None):
        self.calls.append({"url": url, "json": json})
        if self.exc:
            raise self.exc
        return _Resp(self.status)


URL = "https://hooks.example/T0/SECRET"


def test_webhook_channel_posts_minimal_body():
    session = _Session()
    result = WebhookChannel(session=session).send(_message(), URL)
    assert result.outcome == "sent"
    [call] = session.calls
    assert call["url"] == URL
    assert call["json"]["title"] == "Order rejected"
    assert call["json"]["fields"] == {"category": "order", "deep_link": "/orders"}


@pytest.mark.parametrize(
    ("status", "outcome"), [(500, "retry"), (429, "retry"), (404, "dead"), (400, "dead")]
)
def test_webhook_channel_classifies_http_errors_and_redacts(status, outcome):
    result = WebhookChannel(session=_Session(status)).send(_message(), URL)
    assert result.outcome == outcome
    assert "SECRET" not in (result.error or "")


def test_webhook_channel_network_error_retries_redacted():
    exc = requests.ConnectionError(f"Max retries exceeded with url: {URL}")
    result = WebhookChannel(session=_Session(exc=exc)).send(_message(), URL)
    assert result.outcome == "retry"
    assert "SECRET" not in (result.error or "")


def test_delivery_result_constructors():
    assert DeliveryResult.sent().outcome == "sent"
    assert DeliveryResult.retry("x", after=5).retry_after == 5
    assert DeliveryResult.gone("x").outcome == "gone"
    assert DeliveryResult.dead("x").outcome == "dead"


def test_channel_is_abstract():
    with pytest.raises(TypeError):
        Channel()  # type: ignore[abstract]


def test_settings_read_env_and_hide_secrets(monkeypatch):
    monkeypatch.setenv("STONKS_VAPID_PUBLIC_KEY", "pub")
    monkeypatch.setenv("STONKS_VAPID_PRIVATE_KEY", "priv-SECRET")
    monkeypatch.setenv("STONKS_VAPID_SUBJECT", "mailto:ops@example.com")
    monkeypatch.setenv("STONKS_SMTP_HOST", "smtp.example.com")
    monkeypatch.setenv("STONKS_SMTP_PASSWORD", "pw-SECRET")
    monkeypatch.setenv("STONKS_SMTP_FROM", "stonks@example.com")
    monkeypatch.setenv("STONKS_NOTIFY_MAX_ATTEMPTS", "3")
    s = NotifySettings.from_env()
    assert s.webpush.configured and s.smtp.configured
    assert s.outbox.max_attempts == 3
    assert s.smtp.sender == "stonks@example.com"
    assert "SECRET" not in repr(s)
    assert set(s.secrets()) == {"priv-SECRET", "pw-SECRET"}

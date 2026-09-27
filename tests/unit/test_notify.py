"""Unit tests for the Notifier seam (roadmap 2.5a). Hermetic: no HTTP."""

from __future__ import annotations

import json

import requests

from stonks.config import NotifyConfig, WebhookConfig
from stonks.notify import (
    CompositeNotifier,
    LogNotifier,
    Notification,
    Notifier,
    WebhookNotifier,
    build_notifier,
    redact_url,
)

SECRET_URL = "https://hooks.example.test/services/T000/B000/SECRETTOKEN"


class _Resp:
    def __init__(self, status: int = 200) -> None:
        self.status_code = status

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            raise requests.HTTPError(f"{self.status_code} Client Error for url: {SECRET_URL}")


class _FakeSession:
    def __init__(self, resp: _Resp | None = None, exc: Exception | None = None) -> None:
        self.calls: list[dict] = []
        self._resp = resp or _Resp()
        self._exc = exc

    def post(self, url, json=None, timeout=None, headers=None):
        self.calls.append({"url": url, "json": json, "timeout": timeout, "headers": headers})
        if self._exc is not None:
            raise self._exc
        return self._resp


class _Recorder(Notifier):
    def __init__(self) -> None:
        self.sent: list[Notification] = []

    def _send(self, notification: Notification) -> None:
        self.sent.append(notification)


class _Exploding(Notifier):
    def _send(self, notification: Notification) -> None:
        raise RuntimeError("boom")


def _n(level="error", **fields) -> Notification:
    return Notification(level=level, title="tick failed", message="it broke", fields=fields)


def test_redact_url_keeps_only_scheme_and_host():
    assert redact_url(SECRET_URL) == "https://hooks.example.test/***"
    assert "SECRET" not in redact_url("https://user:pw@h.test/x?token=SECRET")
    assert redact_url("not a url") == "***"


def test_webhook_posts_json_with_timeout():
    session = _FakeSession()
    notifier = WebhookNotifier(url=SECRET_URL, timeout_seconds=3.0, session=session)
    notifier.notify(_n(tick_id="t1"))
    [call] = session.calls
    assert call["url"] == SECRET_URL
    assert call["timeout"] == 3.0
    body = call["json"]
    assert body["level"] == "error"
    assert body["title"] == "tick failed"
    assert body["message"] == "it broke"
    assert body["fields"] == {"tick_id": "t1"}
    assert "tick failed" in body["text"]


def _output(capsys) -> str:
    captured = capsys.readouterr()
    return captured.out + captured.err


def test_webhook_never_raises_on_network_error_and_redacts_secret(capsys):
    exc = requests.ConnectionError(f"Max retries exceeded with url: {SECRET_URL}")
    notifier = WebhookNotifier(url=SECRET_URL, session=_FakeSession(exc=exc))
    notifier.notify(_n())  # must not raise
    out = _output(capsys)
    assert "notify.webhook.failed" in out
    assert "SECRETTOKEN" not in out
    assert "hooks.example.test/***" in out


def test_webhook_redacts_path_only_error_messages(capsys):
    # urllib3's MaxRetryError names only the path, not the full URL.
    exc = requests.ConnectionError(
        "HTTPSConnectionPool(host='hooks.example.test', port=443): Max retries exceeded "
        "with url: /services/T000/B000/SECRETTOKEN (Caused by NewConnectionError)"
    )
    WebhookNotifier(url=SECRET_URL, session=_FakeSession(exc=exc)).notify(_n())
    out = _output(capsys)
    assert "notify.webhook.failed" in out
    assert "SECRETTOKEN" not in out


def test_webhook_redacts_query_string_secrets(capsys):
    url = "https://hooks.example.test/hook?token=QUERYSECRET"
    exc = requests.ConnectionError("Max retries exceeded with url: /hook?token=QUERYSECRET")
    WebhookNotifier(url=url, session=_FakeSession(exc=exc)).notify(_n())
    assert "QUERYSECRET" not in _output(capsys)


def test_webhook_never_raises_on_http_error_status(capsys):
    notifier = WebhookNotifier(url=SECRET_URL, session=_FakeSession(resp=_Resp(500)))
    notifier.notify(_n())
    out = _output(capsys)
    assert "notify.webhook.failed" in out
    assert "SECRETTOKEN" not in out


def test_notifier_base_swallows_any_exception(capsys):
    assert _Exploding().notify(_n()) is None  # must not raise
    assert "boom" in _output(capsys)  # the failure is logged, not lost


def test_notification_fields_are_coerced_json_safe():
    session = _FakeSession()
    WebhookNotifier(url=SECRET_URL, session=session).notify(_n(obj=object(), n=1))
    fields = session.calls[0]["json"]["fields"]
    assert fields["n"] == 1
    assert isinstance(fields["obj"], str)


def test_log_notifier_logs_at_level(capsys):
    LogNotifier().notify(_n(level="warning", tick_id="t1"))
    entries = [json.loads(line) for line in _output(capsys).splitlines() if line.startswith("{")]
    [entry] = [e for e in entries if e["event"] == "notify"]
    assert entry["level"] == "warning"
    assert entry["title"] == "tick failed"
    assert entry["tick_id"] == "t1"


def test_composite_filters_by_min_level_and_isolates_children():
    rec = _Recorder()
    composite = CompositeNotifier([_Exploding(), rec], min_level="warning")
    composite.notify(_n(level="info"))
    composite.notify(_n(level="warning"))
    composite.notify(_n(level="error"))
    assert [n.level for n in rec.sent] == ["warning", "error"]


def test_build_notifier_log_only_by_default():
    notifier = build_notifier(NotifyConfig())
    assert isinstance(notifier, CompositeNotifier)
    assert [type(c) for c in notifier.children] == [LogNotifier]


def test_build_notifier_skips_webhook_without_url():
    notifier = build_notifier(NotifyConfig(backends=["log", "webhook"]))
    assert [type(c) for c in notifier.children] == [LogNotifier]


def test_build_notifier_with_webhook():
    cfg = NotifyConfig(backends=["webhook"], webhook=WebhookConfig(url=SECRET_URL))
    notifier = build_notifier(cfg)
    [child] = notifier.children
    assert isinstance(child, WebhookNotifier)
    assert "SECRETTOKEN" not in repr(child)


def test_build_notifier_empty_backends_is_silent():
    notifier = build_notifier(NotifyConfig(backends=[]))
    assert notifier.children == []
    notifier.notify(_n())


def test_build_notifier_with_outbox_routes_to_the_admins(tmp_path):
    from stonks.notify import OutboxNotifier
    from stonks.store.state import SqliteState

    path = tmp_path / "state.sqlite"
    with SqliteState(path) as state:
        state.migrate()
    notifier = build_notifier(NotifyConfig(backends=["outbox"]), state_path=path)
    [child] = notifier.children
    assert isinstance(child, OutboxNotifier)
    notifier.notify(_n(level="error"))
    with SqliteState(path) as state:
        [row] = state.sql("SELECT user_id, category FROM notification_outbox")
    assert (row["user_id"], row["category"]) == ("usr_owner", "system")


def test_build_notifier_skips_outbox_without_a_state_path():
    notifier = build_notifier(NotifyConfig(backends=["outbox"]))
    assert notifier.children == []

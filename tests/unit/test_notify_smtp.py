"""Email channel over SMTP (optional fallback in v1). Hermetic: a fake
``smtplib`` client records what would be sent."""

from __future__ import annotations

import smtplib

import pytest

from stonks.notify.events import Message
from stonks.notify.settings import NotifySettings, OutboxSettings, SmtpSettings
from stonks.notify.smtp import EmailChannel

PASSWORD = "smtp-pw-SECRET"


def _settings(**kw) -> SmtpSettings:
    base = {
        "host": "smtp.example.com",
        "port": 587,
        "username": "stonks",
        "password": PASSWORD,
        "sender": "Stonks <stonks@example.com>",
    }
    base.update(kw)
    return SmtpSettings(**base)


def _message(**kw) -> Message:
    base = {
        "notification_id": 3,
        "user_id": "usr_a",
        "category": "risk",
        "level": "error",
        "urgency": "high",
        "title": "Trading halted",
        "body": "Drawdown limit hit",
        "deep_link": "/risk",
        "dedupe_key": None,
        "ttl_seconds": 86400,
    }
    base.update(kw)
    return Message(**base)


class FakeSMTP:
    instances: list[FakeSMTP] = []
    fail_with: Exception | None = None

    def __init__(self, host, port, timeout=None, **kw):
        self.host, self.port, self.timeout = host, port, timeout
        self.kw = kw
        self.calls: list[str] = []
        self.sent = []
        FakeSMTP.instances.append(self)

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.calls.append("quit")

    def starttls(self, context=None):
        self.calls.append("starttls")

    def login(self, user, password):
        self.calls.append(f"login:{user}")
        assert password == PASSWORD

    def send_message(self, msg):
        if FakeSMTP.fail_with:
            raise FakeSMTP.fail_with
        self.sent.append(msg)


@pytest.fixture(autouse=True)
def _reset():
    FakeSMTP.instances = []
    FakeSMTP.fail_with = None


def _channel(**kw) -> EmailChannel:
    return EmailChannel(
        _settings(**kw), base_url="https://stonks.example.com", smtp_factory=FakeSMTP
    )


def test_unconfigured_is_off():
    assert EmailChannel.from_settings(NotifySettings()) is None
    s = NotifySettings(smtp=_settings(), outbox=OutboxSettings())
    assert EmailChannel.from_settings(s) is not None


def test_sends_a_minimal_plain_text_mail_with_an_absolute_link():
    result = _channel().send(_message(), "alice@example.com")
    assert result.outcome == "sent"
    [smtp] = FakeSMTP.instances
    assert smtp.calls[:2] == ["starttls", "login:stonks"]
    [msg] = smtp.sent
    assert msg["To"] == "alice@example.com"
    assert msg["From"] == "Stonks <stonks@example.com>"
    assert msg["Subject"] == "[Stonks] Trading halted"
    text = msg.get_content()
    assert "Drawdown limit hit" in text
    assert "https://stonks.example.com/risk" in text


def test_ssl_mode_uses_the_ssl_client(monkeypatch):
    used = {}

    class FakeSSL(FakeSMTP):
        def __init__(self, *a, **kw):
            used["ssl"] = True
            super().__init__(*a, **kw)

    ch = EmailChannel(
        _settings(security="ssl", port=465), smtp_factory=FakeSMTP, ssl_factory=FakeSSL
    )
    assert ch.send(_message(), "a@example.com").outcome == "sent"
    assert used == {"ssl": True}
    assert "starttls" not in FakeSMTP.instances[0].calls


def test_header_injection_is_impossible():
    ch = _channel()
    ch.send(_message(title="hi\r\nBcc: victim@example.com"), "a@example.com")
    msg = FakeSMTP.instances[0].sent[0]
    assert msg["Bcc"] is None
    assert "\n" not in msg["Subject"]


def test_bad_recipient_is_dead():
    assert _channel().send(_message(), "not-an-email\r\nBcc: x@y").outcome == "dead"


@pytest.mark.parametrize(
    ("exc", "outcome"),
    [
        (smtplib.SMTPServerDisconnected("gone"), "retry"),
        (smtplib.SMTPResponseException(451, b"try later"), "retry"),
        (smtplib.SMTPResponseException(550, b"no such user"), "dead"),
        (smtplib.SMTPRecipientsRefused({"a@example.com": (550, b"no")}), "dead"),
        (OSError("connection refused"), "retry"),
        (smtplib.SMTPAuthenticationError(535, f"bad {PASSWORD}".encode()), "retry"),
    ],
)
def test_error_classification_and_redaction(exc, outcome):
    FakeSMTP.fail_with = exc
    result = _channel().send(_message(), "a@example.com")
    assert result.outcome == outcome
    assert PASSWORD not in (result.error or "")


@pytest.mark.parametrize(
    "exc",
    [
        smtplib.SMTPRecipientsRefused({"alice@example.com": (550, b"no such user")}),
        smtplib.SMTPResponseException(550, b"5.1.1 <alice@example.com>: user unknown"),
        smtplib.SMTPResponseException(451, b"4.2.0 <alice@example.com> greylisted"),
        OSError("refused sending to alice@example.com"),
    ],
)
def test_errors_never_carry_the_recipient_address(exc):
    # Logs and last_error carry user ids, never email addresses.
    FakeSMTP.fail_with = exc
    result = _channel().send(_message(), "alice@example.com")
    assert result.outcome != "sent"
    assert "alice@example.com" not in (result.error or "")


def test_target_is_the_users_email(tmp_path):
    from stonks.accounts import Role, UserRepository
    from stonks.store.state import SqliteState

    with SqliteState(tmp_path / "s.sqlite") as state:
        state.migrate()
        repo = UserRepository(state)
        a = repo.create(display_name="A", role=Role.TRADER, actor="t", email="a@example.com")
        b = repo.create(display_name="B", role=Role.TRADER, actor="t")
        ch = _channel()
        assert ch.targets(state, a.id) == [""]
        assert ch.resolve(state, a.id, "") == "a@example.com"
        assert ch.targets(state, b.id) == []

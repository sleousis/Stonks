"""Email channel over SMTP (stdlib ``smtplib``): an optional fallback in v1.

Off unless ``STONKS_SMTP_HOST`` and ``STONKS_SMTP_FROM`` are set, and off by
default per user; it is used for ``high`` urgency when the user has no
working push device, or wherever the user turns it on. Mail is plain text
with the same minimal content as a push (title, one line, link), because
mail relays see it too. The SMTP password is never logged.
"""

from __future__ import annotations

import re
import smtplib
import ssl
from collections.abc import Callable
from email.headerregistry import Address
from email.message import EmailMessage
from email.utils import formatdate, make_msgid
from typing import Any

from stonks.ingest.redact import redact_secrets
from stonks.notify.channels import Channel, DeliveryResult, register_channel
from stonks.notify.events import Message, clean_text
from stonks.notify.settings import NotifySettings, SmtpSettings
from stonks.store.state import SqliteState

_EMAIL = re.compile(r"^[^@\s<>,;\"]+@[^@\s<>,;\"]+\.[^@\s<>,;\"]+$")


@register_channel("email")
class EmailChannel(Channel):
    fallback = True

    def __init__(
        self,
        settings: SmtpSettings,
        *,
        base_url: str | None = None,
        smtp_factory: Callable[..., Any] = smtplib.SMTP,
        ssl_factory: Callable[..., Any] = smtplib.SMTP_SSL,
    ) -> None:
        if not settings.configured:
            raise ValueError("email needs STONKS_SMTP_HOST and STONKS_SMTP_FROM")
        self._s = settings
        self._password = settings.password.get_secret_value() if settings.password else None
        self._base_url = (base_url or "").rstrip("/")
        self._smtp_factory = smtp_factory
        self._ssl_factory = ssl_factory

    def __repr__(self) -> str:
        return f"EmailChannel(host={self._s.host!r}, port={self._s.port})"

    @classmethod
    def from_settings(cls, settings: NotifySettings) -> EmailChannel | None:
        if not settings.smtp.configured:
            return None
        return cls(settings.smtp, base_url=settings.outbox.public_base_url)

    def resolve(self, state: SqliteState, user_id: str, target_id: str) -> str | None:
        rows = state.sql("SELECT email FROM users WHERE id = ?", [user_id])
        return rows[0]["email"] if rows and rows[0]["email"] else None

    def _compose(self, message: Message, recipient: str) -> EmailMessage:
        msg = EmailMessage()
        msg["From"] = self._s.sender
        msg["To"] = recipient
        msg["Subject"] = clean_text(f"[Stonks] {message.title}", 120)
        msg["Date"] = formatdate(localtime=False)
        msg["Message-ID"] = make_msgid(domain="stonks")
        if message.urgency == "high":
            msg["X-Priority"] = "1"
        lines = [message.title, "", message.body] if message.body else [message.title]
        if message.deep_link:
            lines += ["", f"Open: {self._base_url}{message.deep_link}"]
        lines += ["", "You get this because email notifications are on in Stonks."]
        msg.set_content("\n".join(lines))
        return msg

    def send(self, message: Message, target: str) -> DeliveryResult:
        if not isinstance(target, str) or not _EMAIL.match(target):
            return DeliveryResult.dead("the user's email address is not valid")
        try:
            Address(addr_spec=target)
            msg = self._compose(message, target)
        except (ValueError, IndexError) as exc:
            return DeliveryResult.dead(self.redact(f"{type(exc).__name__}: {exc}"))
        try:
            self._deliver(msg)
        except smtplib.SMTPRecipientsRefused as exc:
            return DeliveryResult.dead(self.redact(f"recipient refused: {exc}"))
        except smtplib.SMTPAuthenticationError as exc:
            return DeliveryResult.retry(
                self.redact(f"SMTP authentication failed ({exc.smtp_code})")
            )
        except smtplib.SMTPResponseException as exc:
            error = self.redact(f"SMTP {exc.smtp_code}: {exc.smtp_error!r}")
            if 500 <= exc.smtp_code < 600:
                return DeliveryResult.dead(error)
            return DeliveryResult.retry(error)
        except (smtplib.SMTPException, OSError) as exc:
            return DeliveryResult.retry(self.redact(f"{type(exc).__name__}: {exc}"))
        return DeliveryResult.sent()

    def _deliver(self, msg: EmailMessage) -> None:
        s = self._s
        context = ssl.create_default_context()
        if s.security == "ssl":
            client = self._ssl_factory(s.host, s.port, timeout=s.timeout_seconds, context=context)
        else:
            client = self._smtp_factory(s.host, s.port, timeout=s.timeout_seconds)
        with client as smtp:
            if s.security == "starttls":
                smtp.starttls(context=context)
            if s.username and self._password:
                smtp.login(s.username, self._password)
            smtp.send_message(msg)

    def redact(self, text: str, target: Any = None) -> str:
        return redact_secrets(text, [self._password] if self._password else [])

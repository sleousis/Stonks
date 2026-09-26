"""Time-based one-time passwords behind our own seam. Wraps ``pyotp``; no
pyotp type leaves this module.

:class:`SecondFactor` is the seam for more factors later (passkeys). A
code is accepted for the current 30 s step or one step either side, and a
step is never accepted twice (the caller stores the returned step)."""

from __future__ import annotations

import hmac
from abc import ABC, abstractmethod
from datetime import UTC, datetime

import pyotp

_DRIFT_STEPS = 1


class SecondFactor(ABC):
    name: str

    @abstractmethod
    def verify(
        self, secret: str, code: str, *, last_step: int | None, at: datetime | None = None
    ) -> int | None:
        """The accepted step (store it as ``last_step``), or ``None``."""


class Totp(SecondFactor):
    name = "totp"

    def __init__(self, *, issuer: str = "Stonks") -> None:
        self.issuer = issuer

    def new_secret(self) -> str:
        return pyotp.random_base32()

    def provisioning_uri(self, secret: str, account: str) -> str:
        return pyotp.TOTP(secret).provisioning_uri(name=account, issuer_name=self.issuer)

    def verify(
        self, secret: str, code: str, *, last_step: int | None, at: datetime | None = None
    ) -> int | None:
        code = (code or "").strip().replace(" ", "")
        if len(code) != 6 or not code.isdigit():
            return None
        totp = pyotp.TOTP(secret)
        now = at or datetime.now(UTC)
        current = totp.timecode(now)
        for step in range(current - _DRIFT_STEPS, current + _DRIFT_STEPS + 1):
            if last_step is not None and step <= last_step:
                continue
            if hmac.compare_digest(totp.generate_otp(step), code):
                return step
        return None

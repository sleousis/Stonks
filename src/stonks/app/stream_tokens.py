"""Short-lived, job-scoped tokens for event streams.

A browser ``EventSource`` cannot send an ``Authorization`` header, so a UI
that must authenticate reads first asks for a stream token (an authorized
call) and passes it as ``?token=`` on the job's events URL. A token

- names exactly one job and grants nothing else (no other job, no other
  route, no writes);
- names the user it was issued to, so the API refuses it once that user is
  disabled;
- expires ``ttl_seconds`` after it was issued (minutes, not hours), so a
  URL that ends up in a log or browser history is soon worthless;
- is an HMAC-SHA256 over ``job_id`` and expiry with a random per-process
  key: it needs no storage, can't be forged without the key, and every
  token dies with the process that issued it (a restart revokes them all).

Format: ``base64url(json {"j": job_id, "u": user_id, "e": expiry}) "."
base64url(hmac)``.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import secrets
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime

_PURPOSE = b"stonks.job-events.v2:"


@dataclass(frozen=True)
class IssuedStreamToken:
    token: str
    expires_at: datetime


class StreamTokenSigner:
    def __init__(
        self,
        ttl_seconds: float,
        *,
        key: bytes | None = None,
        clock: Callable[[], float] = time.time,
    ) -> None:
        if ttl_seconds <= 0:
            raise ValueError("ttl_seconds must be positive")
        self._ttl = float(ttl_seconds)
        self._key = key or secrets.token_bytes(32)
        self._clock = clock

    def issue(self, job_id: str, user_id: str) -> IssuedStreamToken:
        expiry = int(self._clock() + self._ttl)
        claims = {"j": job_id, "u": user_id, "e": expiry}
        payload = _b64(json.dumps(claims, separators=(",", ":")).encode())
        token = f"{payload}.{_b64(self._sign(payload))}"
        return IssuedStreamToken(token=token, expires_at=datetime.fromtimestamp(expiry, UTC))

    def verify(self, token: str, job_id: str) -> str | None:
        """The user id of an unexpired token this signer issued for
        ``job_id``, else ``None``."""
        try:
            payload, sig = token.split(".")
            if not hmac.compare_digest(_unb64(sig), self._sign(payload)):
                return None
            claims = json.loads(_unb64(payload))
            ok = (
                isinstance(claims, dict)
                and claims.get("j") == job_id
                and isinstance(claims.get("u"), str)
                and isinstance(claims.get("e"), int)
                and self._clock() < claims["e"]
            )
            return claims["u"] if ok else None
        except (ValueError, TypeError, UnicodeDecodeError):
            return None

    def _sign(self, payload: str) -> bytes:
        return hmac.new(self._key, _PURPOSE + payload.encode("ascii"), hashlib.sha256).digest()


def _b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def _unb64(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))

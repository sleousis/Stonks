"""One-time recovery codes for a lost authenticator. Ten per user, 80 bits
each, shown once and stored as SHA-256."""

from __future__ import annotations

import base64
import hashlib
import secrets

RECOVERY_CODE_COUNT = 10


def generate_codes(count: int = RECOVERY_CODE_COUNT) -> list[str]:
    codes: set[str] = set()
    while len(codes) < count:
        raw = base64.b32encode(secrets.token_bytes(10)).decode().lower()  # 16 chars
        codes.add("-".join(raw[i : i + 4] for i in range(0, 16, 4)))
    return sorted(codes)


def normalize(code: str) -> str:
    return "".join(ch for ch in (code or "").lower() if ch.isalnum())


def hash_code(code: str) -> str:
    return hashlib.sha256(normalize(code).encode("ascii", "ignore")).hexdigest()

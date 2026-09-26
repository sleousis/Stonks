"""Random credentials and their stored form.

Session ids, CSRF tokens and API tokens are high-entropy random strings, so
a plain SHA-256 is enough to store them (a slow hash adds nothing).

API token format: ``stk_<id>_<secret>``, where ``<id>`` names the row
(shown in lists) and ``<secret>`` is 32 random bytes in base32. The whole
token is shown once, at creation.
"""

from __future__ import annotations

import base64
import hashlib
import re
import secrets

_TOKEN = re.compile(r"^stk_([a-z0-9]{12})_([A-Z2-7]{52})$")


def random_secret(nbytes: int = 32) -> str:
    return secrets.token_urlsafe(nbytes)


def hash_secret(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def mint_api_token() -> tuple[str, str]:
    """``(token_id, token)``."""
    token_id = secrets.token_hex(6)
    secret = base64.b32encode(secrets.token_bytes(32)).decode().rstrip("=")
    return token_id, f"stk_{token_id}_{secret}"


def parse_api_token(token: str) -> str | None:
    """The token id when ``token`` looks like an API token, else ``None``."""
    match = _TOKEN.match(token or "")
    return match.group(1) if match else None


def looks_like_api_token(token: str) -> bool:
    return (token or "").startswith("stk_")

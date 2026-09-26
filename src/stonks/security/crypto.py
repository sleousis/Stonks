"""Authenticated encryption for secrets at rest (broker credentials, later
TOTP secrets and VAPID keys). Wraps ``cryptography``'s AES-GCM; no
``cryptography`` type leaves this module.

Envelope encryption: every :meth:`SecretBox.seal` draws a fresh 256-bit data
key, encrypts the plaintext with it, and wraps the data key with the active
master key. The master key never touches the database or backups; it comes
from the environment (``STONKS_SECRET_KEYS``) or a key file
(``STONKS_SECRET_KEY_FILE``).

Rotation: a key ring holds one *active* key (the first) and any number of
older keys that can still open. :meth:`SecretBox.rotate` re-wraps a token
under the active key; once nothing is sealed under an old key it can be
dropped from the ring.

Associated data binds a token to where it is stored (e.g.
``broker_credentials:<connection_id>``), so a ciphertext copied onto another
row does not open.

Token format: ``sb1:<key_id>:<base64url(wrap_nonce | wrapped_dek | nonce | ciphertext)>``.
"""

from __future__ import annotations

import base64
import binascii
import os
import re
from dataclasses import dataclass
from pathlib import Path

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

KEYS_ENV = "STONKS_SECRET_KEYS"
KEY_FILE_ENV = "STONKS_SECRET_KEY_FILE"

_VERSION = "sb1"
_KEY_BYTES = 32
_NONCE_BYTES = 12
_TAG_BYTES = 16
_WRAPPED_DEK_BYTES = _KEY_BYTES + _TAG_BYTES
_KEY_ID = re.compile(r"^[A-Za-z0-9_.-]{1,32}$")


class SecretBoxError(ValueError):
    """A token could not be sealed or opened, or the key ring is invalid.
    Messages never contain key material or plaintext."""


class SecretKeyMissing(SecretBoxError):
    """No master key is configured."""


def generate_key() -> str:
    """A fresh master key, base64url-encoded (32 random bytes)."""
    return base64.urlsafe_b64encode(os.urandom(_KEY_BYTES)).decode()


@dataclass(frozen=True)
class Sealed:
    """An opaque sealed token. ``key_id`` is kept visible so stores can find
    tokens that still need rotating."""

    key_id: str
    token: str

    @classmethod
    def parse(cls, token: str) -> Sealed:
        parts = token.split(":", 2) if isinstance(token, str) else []
        if len(parts) != 3 or parts[0] != _VERSION or not _KEY_ID.match(parts[1]):
            raise SecretBoxError("not a sealed token")
        body = _b64decode(parts[2])
        if body is None or len(body) < 2 * _NONCE_BYTES + _WRAPPED_DEK_BYTES + _TAG_BYTES:
            raise SecretBoxError("not a sealed token")
        return cls(key_id=parts[1], token=token)

    def __repr__(self) -> str:  # tokens are opaque; no need to print them
        return f"Sealed(key_id={self.key_id!r})"


class KeyRing:
    """Master keys by id; the first one seals, all of them open."""

    def __init__(self, keys: list[tuple[str, bytes]]) -> None:
        if not keys:
            raise SecretKeyMissing(
                f"no master key configured: set {KEYS_ENV}='<id>:<base64 key>' or "
                f"{KEY_FILE_ENV}=<path>; create one with `python -m stonks.security keygen`"
            )
        seen: dict[str, bytes] = {}
        for key_id, key in keys:
            if not _KEY_ID.match(key_id):
                raise SecretBoxError(f"bad key id {key_id[:32]!r}: letters, digits, _ . - only")
            if key_id in seen:
                raise SecretBoxError(f"duplicate key id {key_id!r}")
            if len(key) != _KEY_BYTES:
                raise SecretBoxError(f"key {key_id!r} must be {_KEY_BYTES} bytes")
            seen[key_id] = key
        self._keys = seen
        self.active_key_id = keys[0][0]

    @classmethod
    def parse(cls, spec: str) -> KeyRing:
        """``"<id>:<base64 key>[,<id>:<base64 key>...]"`` or one pair per
        line; blank lines and ``#`` comments are ignored."""
        pairs: list[tuple[str, bytes]] = []
        for raw in re.split(r"[,\n]", spec or ""):
            item = raw.strip()
            if not item or item.startswith("#"):
                continue
            key_id, sep, encoded = item.partition(":")
            key_id = key_id.strip()
            if not sep:
                raise SecretBoxError(f"key entry for {key_id[:32]!r} must look like '<id>:<key>'")
            key = _b64decode(encoded.strip())
            if key is None:
                raise SecretBoxError(f"key {key_id[:32]!r} is not valid base64")
            pairs.append((key_id, key))
        return cls(pairs)

    def key(self, key_id: str) -> bytes:
        try:
            return self._keys[key_id]
        except KeyError:
            raise SecretBoxError(
                f"token sealed with unknown key {key_id!r}; add it back to {KEYS_ENV}"
            ) from None

    @property
    def key_ids(self) -> tuple[str, ...]:
        return tuple(self._keys)

    def __repr__(self) -> str:
        return f"KeyRing(active={self.active_key_id!r}, ids={list(self._keys)!r})"


class SecretBox:
    def __init__(self, keyring: KeyRing) -> None:
        self._keyring = keyring

    @classmethod
    def from_env(cls, environ: dict[str, str] | None = None) -> SecretBox:
        env = os.environ if environ is None else environ
        spec = env.get(KEYS_ENV)
        if not spec and env.get(KEY_FILE_ENV):
            path = Path(env[KEY_FILE_ENV])
            try:
                spec = path.read_text(encoding="utf-8")
            except OSError as exc:
                raise SecretKeyMissing(
                    f"cannot read {KEY_FILE_ENV} ({path}): {type(exc).__name__}"
                ) from None
        return cls(KeyRing.parse(spec or ""))

    @property
    def active_key_id(self) -> str:
        return self._keyring.active_key_id

    def seal(self, plaintext: bytes, *, aad: str) -> Sealed:
        key_id = self._keyring.active_key_id
        dek = AESGCM.generate_key(bit_length=256)
        wrap_nonce, nonce = os.urandom(_NONCE_BYTES), os.urandom(_NONCE_BYTES)
        wrapped = AESGCM(self._keyring.key(key_id)).encrypt(wrap_nonce, dek, _wrap_aad(key_id, aad))
        ciphertext = AESGCM(dek).encrypt(nonce, bytes(plaintext), aad.encode())
        body = base64.urlsafe_b64encode(wrap_nonce + wrapped + nonce + ciphertext).decode()
        return Sealed(key_id=key_id, token=f"{_VERSION}:{key_id}:{body}")

    def open(self, sealed: Sealed | str, *, aad: str) -> bytes:
        sealed = Sealed.parse(sealed if isinstance(sealed, str) else sealed.token)
        body = _b64decode(sealed.token.split(":", 2)[2]) or b""
        wrap_nonce = body[:_NONCE_BYTES]
        wrapped = body[_NONCE_BYTES : _NONCE_BYTES + _WRAPPED_DEK_BYTES]
        rest = body[_NONCE_BYTES + _WRAPPED_DEK_BYTES :]
        nonce, ciphertext = rest[:_NONCE_BYTES], rest[_NONCE_BYTES:]
        master = self._keyring.key(sealed.key_id)
        try:
            dek = AESGCM(master).decrypt(wrap_nonce, wrapped, _wrap_aad(sealed.key_id, aad))
            return AESGCM(dek).decrypt(nonce, ciphertext, aad.encode())
        except InvalidTag:
            raise SecretBoxError(
                "sealed token failed authentication (wrong key, wrong row or tampered)"
            ) from None

    def needs_rotation(self, sealed: Sealed) -> bool:
        return sealed.key_id != self._keyring.active_key_id

    def rotate(self, sealed: Sealed, *, aad: str) -> Sealed:
        """The same plaintext sealed under the active key."""
        if not self.needs_rotation(sealed):
            return sealed
        return self.seal(self.open(sealed, aad=aad), aad=aad)

    def __repr__(self) -> str:
        return f"SecretBox({self._keyring!r})"


def _wrap_aad(key_id: str, aad: str) -> bytes:
    return f"{_VERSION}|{key_id}|{aad}".encode()


def _b64decode(text: str) -> bytes | None:
    try:
        padded = text + "=" * (-len(text) % 4)
        return base64.urlsafe_b64decode(padded.encode("ascii"))
    except (binascii.Error, ValueError, UnicodeEncodeError):
        return None

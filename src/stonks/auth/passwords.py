"""Password hashing behind our own seam. Wraps ``argon2-cffi`` (argon2id);
no argon2 type leaves this module."""

from __future__ import annotations

from argon2 import PasswordHasher as _Argon2
from argon2.exceptions import InvalidHashError, VerificationError, VerifyMismatchError

MIN_LENGTH = 12
MAX_LENGTH = 256


class PasswordPolicyError(ValueError):
    """The new password is too short, too long or blank."""


def check_policy(password: str) -> None:
    if not isinstance(password, str) or not password.strip():
        raise PasswordPolicyError("password must not be blank")
    if len(password) < MIN_LENGTH:
        raise PasswordPolicyError(f"password must have at least {MIN_LENGTH} characters")
    if len(password) > MAX_LENGTH:
        raise PasswordPolicyError(f"password must have at most {MAX_LENGTH} characters")


class PasswordHasher:
    """argon2id with the library's current defaults unless told otherwise
    (tests pass cheap parameters)."""

    def __init__(
        self,
        *,
        time_cost: int | None = None,
        memory_cost: int | None = None,
        parallelism: int | None = None,
    ) -> None:
        overrides = {
            k: v
            for k, v in (
                ("time_cost", time_cost),
                ("memory_cost", memory_cost),
                ("parallelism", parallelism),
            )
            if v is not None
        }
        self._impl = _Argon2(**overrides)
        # Verified when the account has no password, so a missing account
        # costs the same time as a wrong password.
        self._dummy = self._impl.hash("dummy password for timing")

    def hash(self, password: str) -> str:
        check_policy(password)
        return self._impl.hash(password)

    def verify(self, stored: str | None, password: str) -> bool:
        try:
            return self._impl.verify(stored or self._dummy, password) and stored is not None
        except (VerifyMismatchError, VerificationError, InvalidHashError):
            return False

    def needs_rehash(self, stored: str) -> bool:
        try:
            return self._impl.check_needs_rehash(stored)
        except InvalidHashError:
            return True

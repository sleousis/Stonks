"""The connection seam's value types and the per-provider rate limiter."""

from __future__ import annotations

from datetime import date

import pytest

from stonks.connections.base import (
    Activity,
    Capability,
    ConnectionsError,
    Credentials,
    ExternalPosition,
    RateLimit,
    RateLimited,
    mask_number,
)
from stonks.connections.ratelimit import RateLimiter, limiter_for, reset_limiters


class FakeClock:
    def __init__(self) -> None:
        self.now = 0.0
        self.slept: list[float] = []

    def __call__(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.slept.append(seconds)
        self.now += seconds


def test_credentials_never_print_values():
    creds = Credentials({"api_key": "AKXXXXSECRET", "secret_key": "verysecretvalue"})
    for text in (repr(creds), str(creds)):
        assert "AKXXXXSECRET" not in text
        assert "verysecretvalue" not in text
        assert "api_key" in text
    assert Credentials.from_bytes(creds.to_bytes()) == creds


def test_credentials_redact_scrubs_values_from_text():
    creds = Credentials({"user_secret": "s3cr3t-abcdef"})
    assert creds.redact("GET /x?userSecret=s3cr3t-abcdef failed") == "GET /x?userSecret=*** failed"


def test_credentials_missing_field_names_the_field_only():
    with pytest.raises(ConnectionsError, match="api_key"):
        Credentials({})["api_key"]
    with pytest.raises(ConnectionsError):
        Credentials({"a": 1})  # type: ignore[dict-item]


def test_capability_read_predicate():
    assert Capability.READ_POSITIONS.is_read
    assert not Capability.TRADE.is_read


def test_position_value_prefers_market_value():
    assert ExternalPosition("AAPL", 2, price=10.0).value == 20.0
    assert ExternalPosition("AAPL", 2, price=10.0, market_value=21.0).value == 21.0
    assert ExternalPosition("AAPL", 2).value is None


def test_activity_validates_kind_and_id():
    Activity("a1", "acc", "dividend", date(2026, 1, 2))
    with pytest.raises(ValueError):
        Activity("a1", "acc", "bogus", None)  # type: ignore[arg-type]
    with pytest.raises(ValueError):
        Activity("", "acc", "trade", None)


def test_mask_number_keeps_last_four():
    assert mask_number("123456789") == "…6789"
    assert mask_number(None) is None


def test_rate_limit_must_be_positive():
    with pytest.raises(ValueError):
        RateLimit(0, 1)


def test_limiter_waits_when_the_connection_bucket_is_empty():
    clock = FakeClock()
    limiter = RateLimiter(RateLimit(600, 60), clock=clock, sleep=clock.sleep)
    for _ in range(60):
        limiter.acquire("c1")
    assert clock.slept == []
    limiter.acquire("c1")  # 61st in the same instant: one token takes 1 s
    assert clock.slept == [pytest.approx(1.0)]
    # Another connection has its own bucket.
    limiter.acquire("c2")
    assert len(clock.slept) == 1


def test_limiter_app_bucket_is_shared_across_connections():
    clock = FakeClock()
    limiter = RateLimiter(RateLimit(2, 100), clock=clock, sleep=clock.sleep)
    limiter.acquire("a")
    limiter.acquire("b")
    limiter.acquire("c")
    assert clock.slept == [pytest.approx(30.0)]


def test_limiter_raises_instead_of_waiting_too_long():
    clock = FakeClock()
    limiter = RateLimiter(RateLimit(1, 1), clock=clock, sleep=clock.sleep)
    limiter.acquire("a")
    with pytest.raises(RateLimited) as info:
        limiter.acquire("a", max_wait=5)
    assert info.value.retry_after == pytest.approx(60.0)
    assert info.value.retryable


def test_limiter_for_shares_one_instance_per_provider():
    reset_limiters()
    a = limiter_for("p", RateLimit(10, 5))
    assert limiter_for("p", RateLimit(10, 5)) is a
    assert limiter_for("q", RateLimit(10, 5)) is not a
    reset_limiters()

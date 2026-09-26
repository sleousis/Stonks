"""Short-lived, job-scoped tokens for browser event streams."""

from __future__ import annotations

import pytest

from stonks.app.stream_tokens import StreamTokenSigner


class _Clock:
    def __init__(self, now: float = 1_000_000.0) -> None:
        self.now = now

    def __call__(self) -> float:
        return self.now


def test_issued_token_verifies_for_its_job():
    signer = StreamTokenSigner(ttl_seconds=120, clock=_Clock())
    issued = signer.issue("job_abc")
    assert signer.verify(issued.token, "job_abc")
    assert issued.expires_at.timestamp() == pytest.approx(1_000_120.0)


def test_token_is_scoped_to_one_job():
    signer = StreamTokenSigner(ttl_seconds=120, clock=_Clock())
    token = signer.issue("job_abc").token
    assert not signer.verify(token, "job_other")


def test_token_expires():
    clock = _Clock()
    signer = StreamTokenSigner(ttl_seconds=60, clock=clock)
    token = signer.issue("job_abc").token
    clock.now += 59
    assert signer.verify(token, "job_abc")
    clock.now += 2
    assert not signer.verify(token, "job_abc")


def test_tampered_or_foreign_tokens_are_rejected():
    clock = _Clock()
    signer = StreamTokenSigner(ttl_seconds=60, clock=clock)
    token = signer.issue("job_abc").token
    payload, sig = token.split(".")
    assert not signer.verify(f"{payload}.{sig[:-2]}AA", "job_abc")
    assert not signer.verify(token + "x", "job_abc")
    # another process (fresh random key) can't verify it
    assert not StreamTokenSigner(ttl_seconds=60, clock=clock).verify(token, "job_abc")


@pytest.mark.parametrize("garbage", ["", ".", "abc", "a.b.c", "%%%.###", "e30.e30"])
def test_garbage_is_rejected_without_raising(garbage):
    assert not StreamTokenSigner(ttl_seconds=60).verify(garbage, "job_abc")


def test_ttl_must_be_positive():
    with pytest.raises(ValueError):
        StreamTokenSigner(ttl_seconds=0)

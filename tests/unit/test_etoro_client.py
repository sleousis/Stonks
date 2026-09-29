"""The eToro HTTP client: headers, errors, rate limits and back-off, and
keys kept out of every message and log line (hermetic: a fake eToro)."""

from __future__ import annotations

import logging
import uuid

import pytest

from stonks.connections.base import (
    ProviderAuthError,
    ProviderError,
    ProviderUnavailable,
    RateLimit,
    RateLimited,
)
from stonks.connections.ratelimit import RateLimiter
from stonks.execution.brokers.etoro.client import EtoroClient, OutcomeUnknown
from stonks.execution.brokers.etoro.settings import EtoroConfig
from tests.fakes.etoro_server import API_KEY, USER_KEY, FakeEtoro

PNL = "/api/v1/trading/info/demo/pnl"


class Sleeps:
    def __init__(self) -> None:
        self.waits: list[float] = []

    def __call__(self, seconds: float) -> None:
        self.waits.append(seconds)


def make(fake: FakeEtoro, sleeps: Sleeps | None = None, **config) -> EtoroClient:
    return EtoroClient(
        EtoroConfig(**config),
        api_key=API_KEY,
        user_key=USER_KEY,
        transport=fake.transport(),
        sleep=sleeps or Sleeps(),
    )


def test_every_call_carries_the_key_pair_and_a_fresh_request_id():
    fake = FakeEtoro()
    client = make(fake)
    client.get("pnl", PNL)
    client.get("pnl", PNL)
    first, second = fake.headers_seen
    assert first["x-api-key"] == API_KEY and first["x-user-key"] == USER_KEY
    assert uuid.UUID(first["x-request-id"]) != uuid.UUID(second["x-request-id"])
    assert "authorization" not in first  # never both OAuth and the key pair


def test_a_post_keeps_the_request_id_it_is_given():
    fake = FakeEtoro()
    client = make(fake)
    rid = str(uuid.uuid5(uuid.NAMESPACE_URL, "cid-1"))
    body = {"action": "open", "transaction": "buy", "instrumentId": 1001, "units": 1.0}
    client.post("open", "/api/v2/trading/execution/demo/orders", body, request_id=rid)
    assert fake.headers_seen[-1]["x-request-id"] == rid


def test_refused_keys_are_an_auth_error_that_names_no_key():
    fake = FakeEtoro(user_key="another-user-key-000000")
    with pytest.raises(ProviderAuthError) as info:
        make(fake).get("pnl", PNL)
    assert info.value.status == 401
    assert USER_KEY not in str(info.value) and API_KEY not in str(info.value)


def test_a_key_for_the_other_environment_is_refused():
    fake = FakeEtoro(env="demo")
    with pytest.raises(ProviderAuthError) as info:
        make(fake).get("pnl", "/api/v1/trading/info/real/pnl")
    assert info.value.status == 403


def test_a_vendor_message_that_echoes_a_key_is_scrubbed():
    fake = FakeEtoro()
    fake.queue(PNL, 400, body={"errorCode": "Bad", "errorMessage": f"bad key {USER_KEY}"})
    with pytest.raises(ProviderError) as info:
        make(fake).get("pnl", PNL)
    assert USER_KEY not in str(info.value)
    assert "***" in str(info.value)
    assert info.value.status == 400


def test_429_backs_off_honouring_retry_after_then_succeeds():
    fake = FakeEtoro()
    fake.queue(PNL, 429, {"Retry-After": "7"})
    fake.queue(PNL, 429)
    sleeps = Sleeps()
    out = make(fake, sleeps, backoff_seconds=2.0).get("pnl", PNL)
    assert "clientPortfolio" in out
    assert sleeps.waits == [7.0, 4.0]  # the hint, then 2 * 2**1


def test_429_gives_up_after_the_retries():
    fake = FakeEtoro()
    for _ in range(4):
        fake.queue(PNL, 429)
    sleeps = Sleeps()
    with pytest.raises(RateLimited):
        make(fake, sleeps, max_retries=2, backoff_seconds=1.0).get("pnl", PNL)
    assert sleeps.waits == [1.0, 2.0]
    assert len(fake.calls) == 3


def test_backoff_is_capped_at_a_minute():
    fake = FakeEtoro()
    fake.queue(PNL, 429, {"Retry-After": "3600"})
    sleeps = Sleeps()
    make(fake, sleeps).get("pnl", PNL)
    assert sleeps.waits == [60.0]


def test_reads_retry_a_server_error_but_orders_do_not():
    fake = FakeEtoro()
    fake.queue(PNL, 503)
    assert "clientPortfolio" in make(fake).get("pnl", PNL)

    path = "/api/v2/trading/execution/demo/orders"
    fake.queue(path, 502)
    body = {"action": "open", "transaction": "buy", "instrumentId": 1001, "units": 1.0}
    with pytest.raises(OutcomeUnknown):
        make(fake).post("open", path, body, request_id=str(uuid.uuid4()))
    assert fake.calls.count(("POST", path)) == 1


def test_server_errors_on_reads_become_unavailable_after_retries():
    fake = FakeEtoro()
    for _ in range(3):
        fake.queue(PNL, 500)
    with pytest.raises(ProviderUnavailable):
        make(fake, max_retries=2).get("pnl", PNL)


def test_our_own_budgets_pace_reads_and_orders_apart():
    fake = FakeEtoro()
    ticks = [0.0]
    sleeps: list[float] = []

    def sleep(s: float) -> None:
        sleeps.append(s)
        ticks[0] += s

    reads = RateLimiter(RateLimit(1000, 2), clock=lambda: ticks[0], sleep=sleep)
    writes = RateLimiter(RateLimit(1000, 1), clock=lambda: ticks[0], sleep=sleep)
    client = EtoroClient(
        EtoroConfig(), api_key=API_KEY, user_key=USER_KEY, transport=fake.transport(),
        read_limiter=reads, write_limiter=writes, connection_key="con_1", sleep=sleep,
    )  # fmt: skip
    client.get("pnl", PNL)
    client.get("pnl", PNL)
    assert sleeps == []
    client.get("pnl", PNL)  # the third read in a minute waits
    assert sleeps and sleeps[0] == pytest.approx(30.0)
    path = "/api/v2/trading/execution/demo/orders"
    body = {"action": "open", "transaction": "buy", "instrumentId": 1001, "units": 1.0}
    client.post("open", path, body, request_id=str(uuid.uuid4()))
    before = len(sleeps)
    client.post("open", path, dict(body), request_id=str(uuid.uuid4()))
    assert len(sleeps) == before + 1  # one order a minute


def test_keys_never_reach_repr_or_logs(caplog):
    fake = FakeEtoro()
    fake.queue(PNL, 429)
    client = make(fake)
    with caplog.at_level(logging.DEBUG):
        client.get("pnl", PNL)
    text = repr(client) + caplog.text
    assert API_KEY not in text and USER_KEY not in text


def test_network_failures_are_unavailable_without_detail():
    import httpx2

    def boom(request):
        raise httpx2.ConnectError(f"cannot reach {request.headers['x-user-key']}")

    client = EtoroClient(EtoroConfig(max_retries=0), api_key=API_KEY, user_key=USER_KEY,
                         transport=httpx2.MockTransport(boom), sleep=Sleeps())  # fmt: skip
    with pytest.raises(ProviderUnavailable) as info:
        client.get("pnl", PNL)
    assert USER_KEY not in str(info.value)

"""The MCP server's HTTP client over the REST API (hermetic, MockTransport)."""

from __future__ import annotations

import json

import httpx2
import pytest

from stonks.mcp.client import ApiClient, ApiError, ApiUnavailableError, segment

TOKEN = "s3cret-token-value"
BASE = "http://127.0.0.1:8000"


@pytest.fixture
def anyio_backend():
    return "asyncio"


def _client(handler, *, token: str | None = TOKEN, base_url: str = BASE) -> ApiClient:
    return ApiClient(base_url, token=token, transport=httpx2.MockTransport(handler))


@pytest.mark.anyio
async def test_get_returns_json_and_drops_none_params():
    seen = {}

    def handler(request: httpx2.Request) -> httpx2.Response:
        seen["url"] = str(request.url)
        return httpx2.Response(200, json={"items": [], "total": 0})

    api = _client(handler)
    body = await api.get("/api/strategies", {"status": None, "limit": 5})
    assert body == {"items": [], "total": 0}
    assert seen["url"] == f"{BASE}/api/strategies?limit=5"


@pytest.mark.anyio
async def test_token_is_sent_as_bearer():
    seen = {}

    def handler(request: httpx2.Request) -> httpx2.Response:
        seen["auth"] = request.headers.get("authorization")
        seen["body"] = json.loads(request.content)
        return httpx2.Response(202, json={"id": "j1"})

    api = _client(handler)
    assert await api.post("/api/ingest/runs", {"kind": "prices"}) == {"id": "j1"}
    assert seen == {"auth": f"Bearer {TOKEN}", "body": {"kind": "prices"}}


@pytest.mark.anyio
async def test_post_without_token_fails_before_sending():
    calls = []

    def handler(request: httpx2.Request) -> httpx2.Response:
        calls.append(request)
        return httpx2.Response(202, json={})

    api = _client(handler, token=None)
    with pytest.raises(ApiError, match="STONKS_API_TOKEN"):
        await api.post("/api/ticks", {})
    assert calls == []


@pytest.mark.anyio
async def test_get_without_token_sends_no_auth_header():
    seen = {}

    def handler(request: httpx2.Request) -> httpx2.Response:
        seen["auth"] = request.headers.get("authorization")
        return httpx2.Response(200, json={})

    await _client(handler, token=None).get("/api/portfolio")
    assert seen["auth"] is None


@pytest.mark.anyio
async def test_problem_details_become_api_error():
    def handler(request: httpx2.Request) -> httpx2.Response:
        return httpx2.Response(
            404,
            json={"title": "Not Found", "status": 404, "detail": "no strategy with id 'x'"},
            headers={"content-type": "application/problem+json"},
        )

    with pytest.raises(ApiError) as exc:
        await _client(handler).get("/api/strategies/x")
    assert exc.value.status == 404
    assert "no strategy with id 'x'" in str(exc.value)


@pytest.mark.anyio
async def test_401_explains_the_token():
    def handler(request: httpx2.Request) -> httpx2.Response:
        return httpx2.Response(401, json={"title": "Unauthorized", "status": 401})

    with pytest.raises(ApiError, match="STONKS_API_TOKEN") as exc:
        await _client(handler).post("/api/ticks", {})
    assert TOKEN not in str(exc.value)


@pytest.mark.anyio
async def test_validation_errors_are_listed():
    def handler(request: httpx2.Request) -> httpx2.Response:
        return httpx2.Response(
            422,
            json={
                "title": "Invalid request",
                "status": 422,
                "detail": "validation failed",
                "errors": [{"loc": ["body", "universe"], "msg": "too short"}],
            },
        )

    with pytest.raises(ApiError, match="body.universe: too short"):
        await _client(handler).post("/api/lab/backtests", {})


@pytest.mark.anyio
async def test_non_json_error_body_is_not_echoed_raw():
    def handler(request: httpx2.Request) -> httpx2.Response:
        return httpx2.Response(502, text="<html>" + "x" * 5000 + "</html>")

    with pytest.raises(ApiError) as exc:
        await _client(handler).get("/api/portfolio")
    assert exc.value.status == 502
    assert len(str(exc.value)) < 300


@pytest.mark.anyio
async def test_unreachable_api_tells_user_to_run_serve():
    def handler(request: httpx2.Request) -> httpx2.Response:
        raise httpx2.ConnectError("connection refused", request=request)

    with pytest.raises(ApiUnavailableError, match="stonks serve") as exc:
        await _client(handler).get("/api/portfolio")
    assert BASE in str(exc.value)


@pytest.mark.anyio
async def test_timeout_is_unavailable_too():
    def handler(request: httpx2.Request) -> httpx2.Response:
        raise httpx2.ReadTimeout("slow", request=request)

    with pytest.raises(ApiUnavailableError, match="stonks serve"):
        await _client(handler).get("/api/portfolio")


def test_token_refused_over_plain_http_to_remote_host():
    with pytest.raises(ValueError, match="https"):
        ApiClient("http://203.0.113.7:8000", token=TOKEN)
    # https or loopback is fine; no token is fine anywhere
    ApiClient("https://stonks.example.com", token=TOKEN)
    ApiClient("http://localhost:8000", token=TOKEN)
    ApiClient("http://[::1]:8000", token=TOKEN)
    ApiClient("http://203.0.113.7:8000", token=None)


def test_credentials_in_api_url_are_refused():
    # they would otherwise show up in logs and "not reachable" errors
    with pytest.raises(ValueError, match="STONKS_API_TOKEN"):
        ApiClient("http://user:pw@127.0.0.1:8000")


def test_repr_hides_token():
    api = ApiClient(BASE, token=TOKEN)
    assert TOKEN not in repr(api)
    assert api.has_token
    assert api.redact(f"oops {TOKEN} leaked") == "oops *** leaked"


@pytest.mark.parametrize(
    "bad", ["..", ".", "../ticks", "a/b", "x#frag", "x?y=1", "a b", "", "x%2Fy", "..\\ticks"]
)
def test_segment_rejects_path_tricks(bad):
    with pytest.raises(ApiError, match="invalid id"):
        segment(bad)


@pytest.mark.parametrize("ok", ["bah_active", "tick_2026-03-20_33ef4fad", "a1b2c3", "m.v2:x"])
def test_segment_accepts_ids(ok):
    assert segment(ok) == ok

"""The remote lab worker's API client and its command line (roadmap 14.9)."""

from __future__ import annotations

import httpx2
import pytest

from stonks.lab.offload.__main__ import main
from stonks.lab.offload.api_client import (
    LabWorkerApiClient,
    LabWorkerApiError,
    LabWorkerApiUnavailable,
)


def test_the_token_never_goes_over_plain_http_to_an_untrusted_host():
    with pytest.raises(ValueError, match="plain http"):
        LabWorkerApiClient("http://stonks.example.com", token="t")
    LabWorkerApiClient("https://stonks.example.com", token="t")
    LabWorkerApiClient("http://127.0.0.1:8000", token="t")
    LabWorkerApiClient(
        "http://server.tailnet.ts.net", token="t", trusted_hosts=["server.tailnet.ts.net"]
    )


@pytest.mark.parametrize(
    ("url", "token"), [("https://x.example", ""), ("https://u:p@x.example", "t"), ("ftp://x", "t")]
)
def test_bad_settings_are_refused(url, token):
    with pytest.raises(ValueError):
        LabWorkerApiClient(url, token=token)


def _client(handler) -> LabWorkerApiClient:
    http = httpx2.Client(base_url="https://x.example", transport=httpx2.MockTransport(handler))
    return LabWorkerApiClient("https://x.example", token="sekret-token", http=http)


def test_requests_carry_the_token_and_errors_hide_it():
    seen = {}

    def handler(request: httpx2.Request) -> httpx2.Response:
        seen["auth"] = request.headers.get("authorization")
        seen["path"] = request.url.path
        return httpx2.Response(409, json={"title": "Conflict", "detail": "echo sekret-token back"})

    client = _client(handler)
    with pytest.raises(LabWorkerApiError) as err:
        client.claim("w1")
    assert seen == {"auth": "Bearer sekret-token", "path": "/api/lab/worker/claim"}
    assert err.value.status == 409
    assert "sekret-token" not in str(err.value) and "***" in str(err.value)
    assert "sekret-token" not in repr(client)


def test_an_unreachable_api_is_its_own_error():
    def handler(request: httpx2.Request) -> httpx2.Response:
        raise httpx2.ConnectError("down", request=request)

    with pytest.raises(LabWorkerApiUnavailable):
        _client(handler).claim("w1")


def test_the_worker_command_needs_the_token(monkeypatch, tmp_path, capsys):
    monkeypatch.delenv("STONKS_LAB_WORKER_TOKEN", raising=False)
    monkeypatch.setenv("STONKS_DATA_DIR", str(tmp_path))
    code = main(["--config", str(tmp_path / "none.toml"), "worker", "--api", "https://x.example"])
    assert code == 1
    assert "STONKS_LAB_WORKER_TOKEN" in capsys.readouterr().err

"""EodhdDataSource HTTP hardening: API-key redaction, retry policy, and
executor reuse. Hermetic — every request goes through a fake session."""

from __future__ import annotations

import json
from typing import Any

import pytest
import requests

from stonks.ingest.sources import eodhd
from stonks.ingest.sources.eodhd import EodhdDataSource

SECRET = "SuPeRsEcReTkEy123"


class _Response:
    def __init__(self, status: int, body: Any, url: str = ""):
        self.status_code = status
        self._body = body
        self.url = url

    @property
    def text(self) -> str:
        return self._body if isinstance(self._body, str) else json.dumps(self._body)

    def json(self) -> Any:
        if isinstance(self._body, str):
            return json.loads(self._body)
        return self._body

    def raise_for_status(self) -> None:
        # Mirror requests' real message shape, which embeds the full URL
        # (query string included) — the source of the leak.
        if self.status_code >= 400:
            raise requests.HTTPError(
                f"{self.status_code} Client Error: boom for url: {self.url}", response=self
            )


class _ScriptedSession:
    """Returns (or raises) each scripted item in turn; the last repeats."""

    def __init__(self, script: list[Any]):
        self._script = script
        self.calls = 0

    def get(self, url, params=None, timeout=None):
        full_url = f"{url}?" + "&".join(f"{k}={v}" for k, v in (params or {}).items())
        item = self._script[min(self.calls, len(self._script) - 1)]
        self.calls += 1
        if isinstance(item, BaseException):
            raise item
        if callable(item):
            return item(full_url)
        return item


def _status(code: int, body: Any = "err"):
    return lambda full_url: _Response(code, body, url=full_url)


def _conn_error(full_url: str):
    raise requests.ConnectionError(f"Max retries exceeded with url: {full_url}")


@pytest.fixture
def no_sleep(monkeypatch):
    sleeps: list[float] = []
    monkeypatch.setattr(eodhd.time, "sleep", lambda s: sleeps.append(s))
    return sleeps


def _source(session, max_retries: int = 3) -> EodhdDataSource:
    return EodhdDataSource(
        api_key=SECRET,
        max_retries=max_retries,
        retry_backoff_seconds=1.0,
        session=session,  # type: ignore[arg-type]
    )


# ---------------------------------------------------------------- redaction


def test_http_error_message_never_contains_api_key(no_sleep, capsys):
    session = _ScriptedSession([_status(404)])
    with pytest.raises(requests.HTTPError) as info:
        _source(session).fetch_prices("AAPL.US")
    assert SECRET not in str(info.value)
    assert SECRET not in repr(info.value.args)
    assert SECRET not in capsys.readouterr().out


def test_connection_error_after_retries_never_contains_api_key(no_sleep, capsys):
    session = _ScriptedSession([_conn_error])
    with pytest.raises(requests.ConnectionError) as info:
        _source(session).fetch_prices("AAPL.US")
    assert SECRET not in str(info.value)
    out = capsys.readouterr().out
    assert "eodhd.request.failed" in out
    assert SECRET not in out


def test_redacted_chained_exceptions_do_not_leak_key(no_sleep):
    def _chained(full_url: str):
        try:
            raise OSError(f"low-level failure for {full_url}")
        except OSError as inner:
            raise requests.ConnectionError(f"wrapped: {full_url}") from inner

    session = _ScriptedSession([_chained])
    with pytest.raises(requests.ConnectionError) as info:
        _source(session, max_retries=1).fetch_prices("AAPL.US")
    exc: BaseException | None = info.value
    while exc is not None:
        assert SECRET not in str(exc)
        exc = exc.__cause__ or exc.__context__


def test_metadata_soft_fail_log_never_contains_api_key(no_sleep, capsys):
    session = _ScriptedSession([_conn_error])
    with pytest.raises(eodhd.EodhdAllEndpointsFailedError):
        _source(session, max_retries=1).fetch_metadata("AAPL.US")
    out = capsys.readouterr().out
    assert "eodhd.metadata.skipped_error" in out
    assert SECRET not in out


def test_redact_secrets_masks_explicit_secret_and_token_params():
    from stonks.ingest.redact import redact_secrets

    text = f"url: https://x/api/eod/A?fmt=json&api_token={SECRET}&apikey=other"
    out = redact_secrets(text, secrets=(SECRET,))
    assert SECRET not in out
    assert "other" not in out
    assert "fmt=json" in out


# ---------------------------------------------------------------- retry policy


@pytest.mark.parametrize("code", [400, 401, 404, 422])
def test_4xx_raises_immediately_without_retry(no_sleep, code):
    session = _ScriptedSession([_status(code)])
    with pytest.raises(requests.HTTPError):
        _source(session).fetch_prices("AAPL.US")
    assert session.calls == 1
    assert no_sleep == []


def test_403_still_maps_to_free_tier_without_retry(no_sleep):
    session = _ScriptedSession([_status(403, "Only EOD data allowed")])
    with pytest.raises(eodhd.EodhdFreeTierError):
        _source(session).fetch_prices("AAPL.US")
    assert session.calls == 1


@pytest.mark.parametrize("code", [429, 500, 502, 503])
def test_429_and_5xx_are_retried_then_succeed(no_sleep, code):
    ok = _Response(200, [])
    session = _ScriptedSession([_status(code), ok])
    assert _source(session).fetch_prices("AAPL.US") == []
    assert session.calls == 2
    assert no_sleep == [1.0]


def test_5xx_exhausts_retries_and_raises(no_sleep):
    session = _ScriptedSession([_status(503)])
    with pytest.raises(requests.HTTPError):
        _source(session).fetch_prices("AAPL.US")
    assert session.calls == 3
    assert no_sleep == [1.0, 2.0]


@pytest.mark.parametrize(
    "exc", [requests.ConnectionError("down"), requests.Timeout("slow")], ids=["conn", "timeout"]
)
def test_transport_errors_are_retried(no_sleep, exc):
    session = _ScriptedSession([exc, _Response(200, [])])
    assert _source(session).fetch_prices("AAPL.US") == []
    assert session.calls == 2


def test_programming_errors_are_not_retried(no_sleep):
    session = _ScriptedSession([TypeError("bug")])
    with pytest.raises(TypeError):
        _source(session).fetch_prices("AAPL.US")
    assert session.calls == 1
    assert no_sleep == []


def test_non_json_body_is_not_retried(no_sleep):
    session = _ScriptedSession([_Response(200, "<html>not json</html>")])
    with pytest.raises(ValueError):
        _source(session).fetch_prices("AAPL.US")
    assert session.calls == 1

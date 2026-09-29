"""JSON calls against eToro's official public API, paced and retried.

Plain HTTP through ``httpx2`` (like the SnapTrade client): the few
endpoints Stonks uses fit one small client, and vendor JSON never leaves
the ``etoro`` package. Every call carries the key pair (``x-api-key``,
``x-user-key``) and an ``x-request-id``: a fresh UUID for a read, the
caller's fixed one for an order, which makes a repeat idempotent.

The terms ask a trading tool for its own rate limiting, an order
frequency cap and back-off (Builders' Economy terms, Part II 2.5):

- reads take a token from ``read_limiter`` and orders from
  ``write_limiter`` (one bucket per connection each, set below eToro's
  60 and 20 a minute)
- a ``429`` is retried after the ``Retry-After`` hint, else an
  exponential back-off, up to ``max_retries``, capped at a minute
- a read that meets a network error or a ``5xx`` is retried the same way.
  An order is never resent after one, since it may have gone through:
  :class:`OutcomeUnknown` tells the caller to look it up instead.

Errors never carry a key: messages are built from the status and eToro's
short error text, scrubbed of both keys. Headers are never logged.
"""

from __future__ import annotations

import time
import uuid
from collections.abc import Callable, Mapping
from typing import Any

import httpx2

from stonks.connections.base import (
    ProviderAuthError,
    ProviderError,
    ProviderUnavailable,
    RateLimited,
)
from stonks.connections.ratelimit import RateLimiter
from stonks.execution.brokers.etoro._json import obj
from stonks.execution.brokers.etoro.settings import EtoroConfig
from stonks.ingest.redact import redact_secrets
from stonks.logging import get_logger

_log = get_logger("stonks.execution.brokers.etoro")

_MAX_DETAIL = 160
_MAX_BACKOFF = 60.0
_READ_WAIT = 30.0
_WRITE_WAIT = 65.0


class OutcomeUnknown(ProviderUnavailable):
    """An order call failed after it may have reached eToro (a timeout, a
    dropped link, a ``5xx``). Look the order up before sending anything
    again."""


class EtoroClient:
    """Signed, paced JSON calls. Knows nothing about Stonks' types."""

    def __init__(
        self,
        config: EtoroConfig,
        *,
        api_key: str,
        user_key: str,
        transport: Any = None,
        read_limiter: RateLimiter | None = None,
        write_limiter: RateLimiter | None = None,
        connection_key: str = "",
        sleep: Callable[[float], None] | None = None,
    ) -> None:
        self._config = config
        self._api_key = api_key
        self._user_key = user_key
        self._reads = read_limiter
        self._writes = write_limiter
        self._connection_key = connection_key
        self._sleep = sleep or time.sleep
        self._base = config.base_url.rstrip("/")
        self._http = httpx2.Client(transport=transport, timeout=config.timeout_seconds)

    def __repr__(self) -> str:
        return f"EtoroClient(base={self._base!r})"

    def close(self) -> None:
        self._http.close()

    def redact(self, text: str) -> str:
        return redact_secrets(text, [self._api_key, self._user_key])

    # ---- verbs ---------------------------------------------------------------------------

    def get(self, op: str, path: str, params: Mapping[str, Any] | None = None) -> Any:
        return self._call(op, "GET", path, params=params, request_id=None, write=False)

    def post_read(self, op: str, path: str, body: Mapping[str, Any]) -> Any:
        """A POST that only reads (eligibility): paced and retried like a
        read, and counted in eToro's order budget too, which it shares."""
        return self._call(op, "POST", path, body=body, request_id=None, write=True, safe=True)

    def post(self, op: str, path: str, body: Mapping[str, Any], *, request_id: str) -> Any:
        return self._call(op, "POST", path, body=body, request_id=request_id, write=True)

    def delete(self, op: str, path: str, *, request_id: str | None = None) -> Any:
        return self._call(op, "DELETE", path, request_id=request_id, write=True)

    # ---- plumbing ------------------------------------------------------------------------

    def _call(
        self,
        op: str,
        method: str,
        path: str,
        *,
        params: Mapping[str, Any] | None = None,
        body: Mapping[str, Any] | None = None,
        request_id: str | None,
        write: bool,
        safe: bool | None = None,
    ) -> Any:
        #: a call that changes nothing may be retried after any failure
        retry_any = (not write) if safe is None else safe
        attempt = 0
        while True:
            self._acquire(write)
            rid = request_id or str(uuid.uuid4())
            headers = {
                "x-api-key": self._api_key,
                "x-user-key": self._user_key,
                "x-request-id": rid,
                "Accept": "application/json",
            }
            try:
                response = self._http.request(
                    method,
                    f"{self._base}{path}",
                    params={k: v for k, v in (params or {}).items() if v is not None},
                    json=dict(body) if body is not None else None,
                    headers=headers,
                )
            except httpx2.HTTPError as exc:
                failure: ProviderError = (
                    ProviderUnavailable(f"etoro {op} failed: {type(exc).__name__}")
                    if retry_any
                    else OutcomeUnknown(f"etoro {op}: no answer ({type(exc).__name__})")
                )
                if retry_any and attempt < self._config.max_retries:
                    attempt += 1
                    self._back_off(op, attempt, None, "network")
                    continue
                raise failure from None
            status = response.status_code
            if status < 400:
                return _json(response)
            if status == 429 and attempt < self._config.max_retries:
                attempt += 1
                self._back_off(op, attempt, _retry_after(response), "rate_limited")
                continue
            if status >= 500 and retry_any and attempt < self._config.max_retries:
                attempt += 1
                self._back_off(op, attempt, None, "server_error")
                continue
            raise self._error(op, response, retry_any)

    def _acquire(self, write: bool) -> None:
        limiter = self._writes if write else self._reads
        if limiter is not None:
            limiter.acquire(self._connection_key, max_wait=_WRITE_WAIT if write else _READ_WAIT)

    def _back_off(self, op: str, attempt: int, hint: float | None, why: str) -> None:
        wait = hint if hint is not None else self._config.backoff_seconds * 2 ** (attempt - 1)
        wait = min(max(wait, 0.0), _MAX_BACKOFF)
        _log.info("etoro.retry", op=op, attempt=attempt, wait_seconds=wait, reason=why)
        self._sleep(wait)

    def _error(self, op: str, response: httpx2.Response, retry_any: bool) -> ProviderError:
        status = response.status_code
        detail = self.redact(_detail(response))
        message = f"etoro {op} failed (HTTP {status})" + (f": {detail}" if detail else "")
        if status in (401, 403):
            return ProviderAuthError(message, status=status)
        if status == 429:
            return RateLimited(message, retry_after=_retry_after(response))
        if status >= 500:
            if not retry_any:
                return OutcomeUnknown(message, status=status)
            return ProviderUnavailable(message, status=status)
        return ProviderError(message, status=status)


def _json(response: httpx2.Response) -> Any:
    if not response.content:
        return {}
    try:
        return response.json()
    except ValueError:
        raise ProviderError(
            f"eToro sent a reply that is not JSON (HTTP {response.status_code})"
        ) from None


def _retry_after(response: httpx2.Response) -> float | None:
    raw = response.headers.get("Retry-After")
    if raw is None:
        return None
    try:
        return max(float(raw), 0.0)
    except ValueError:
        return None


def _detail(response: httpx2.Response) -> str:
    """eToro's short error text, when the body carries one."""
    try:
        data = obj(response.json())
    except ValueError:
        return ""
    for key in ("errorMessage", "message", "error", "title"):
        value = data.get(key)
        if isinstance(value, str) and value.strip():
            text = " ".join(value.split())
            return text if len(text) <= _MAX_DETAIL else text[: _MAX_DETAIL - 1] + "…"
    return ""


__all__ = ["EtoroClient", "OutcomeUnknown"]

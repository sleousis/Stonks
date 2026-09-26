"""Small async HTTP client over the Stonks REST API.

The MCP server never opens the lake (DuckDB allows one writing process and
``stonks serve`` holds it), so every tool goes through this client. It
knows nothing about MCP: errors are plain :class:`ApiError` subclasses the
server layer turns into tool errors.
"""

from __future__ import annotations

import ipaddress
import re
from typing import Any
from urllib.parse import urlsplit

import httpx2

_LOOPBACK_NAMES = frozenset({"localhost"})
_MAX_DETAIL = 200


class ApiError(Exception):
    """The API answered with an error (or a request could not be made)."""

    def __init__(self, message: str, *, status: int | None = None) -> None:
        super().__init__(message)
        self.status = status


class ApiUnavailableError(ApiError):
    """The API could not be reached at all."""


_SEGMENT = re.compile(r"[A-Za-z0-9_.:@+-]{1,200}")


def segment(value: str) -> str:
    """Validate an id interpolated into a URL path.

    Without this a crafted id such as ``../ticks#`` would turn
    ``POST /api/strategies/{id}/promote`` into ``POST /api/ticks``.
    """
    if not _SEGMENT.fullmatch(value) or value in (".", ".."):
        raise ApiError(f"invalid id {value[:60]!r}: letters, digits and _ . : @ + - only")
    return value


def _is_loopback(host: str | None) -> bool:
    if not host:
        return False
    if host.lower() in _LOOPBACK_NAMES:
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


class ApiClient:
    """Typed-enough wrapper: ``get``/``post`` return decoded JSON.

    The bearer token is sent when configured (reads need it too when the
    server has ``open_reads_on_loopback`` off). ``post``/``patch`` refuse to send a
    request without one, since every mutating route requires it.
    """

    def __init__(
        self,
        base_url: str,
        *,
        token: str | None = None,
        timeout: float = 30.0,
        transport: httpx2.AsyncBaseTransport | None = None,
    ) -> None:
        parts = urlsplit(base_url)
        if parts.username or parts.password:
            raise ValueError("credentials in api_url are not supported; use STONKS_API_TOKEN")
        if token and parts.scheme != "https" and not _is_loopback(parts.hostname):
            raise ValueError(
                f"refusing to send STONKS_API_TOKEN over plain http to {parts.hostname!r}; "
                "use https or a loopback api_url"
            )
        self.base_url = base_url.rstrip("/")
        self._token = token or None
        headers = {"Authorization": f"Bearer {token}"} if token else {}
        self._http = httpx2.AsyncClient(
            base_url=self.base_url, headers=headers, timeout=timeout, transport=transport
        )

    def __repr__(self) -> str:
        return f"ApiClient(base_url={self.base_url!r}, token={'***' if self._token else None})"

    @property
    def has_token(self) -> bool:
        return self._token is not None

    def redact(self, text: str) -> str:
        return text.replace(self._token, "***") if self._token else text

    async def aclose(self) -> None:
        await self._http.aclose()

    async def get(self, path: str, params: dict[str, Any] | None = None) -> Any:
        clean = {k: v for k, v in (params or {}).items() if v is not None}
        return await self._request("GET", path, params=clean)

    async def post(self, path: str, body: dict[str, Any] | None = None) -> Any:
        self._require_token()
        return await self._request("POST", path, json=body if body is not None else {})

    async def patch(self, path: str, body: dict[str, Any]) -> Any:
        self._require_token()
        return await self._request("PATCH", path, json=body)

    def _require_token(self) -> None:
        if not self.has_token:
            raise ApiError(
                "STONKS_API_TOKEN is not set: write and job tools need the same token "
                "`stonks serve` was started with"
            )

    async def _request(self, method: str, path: str, **kwargs: Any) -> Any:
        try:
            resp = await self._http.request(method, path, **kwargs)
        except (httpx2.TransportError, httpx2.TimeoutException) as exc:
            raise ApiUnavailableError(
                f"Stonks API not reachable at {self.base_url} ({type(exc).__name__}). "
                "Start it with `stonks serve` (or fix [mcp].api_url)."
            ) from None
        if resp.status_code >= 400:
            raise ApiError(self._error_message(resp), status=resp.status_code)
        if not resp.content:
            return None
        return resp.json()

    def _error_message(self, resp: httpx2.Response) -> str:
        status = resp.status_code
        try:
            body = resp.json()
        except ValueError:
            body = None
        if not isinstance(body, dict):
            return f"API error {status} (non-JSON response)"
        title = str(body.get("title") or "API error")
        detail = body.get("detail")
        msg = f"{title} ({status})"
        if detail:
            msg += f": {str(detail)[:_MAX_DETAIL]}"
        errors = body.get("errors")
        if isinstance(errors, list) and errors:
            listed = "; ".join(
                f"{'.'.join(str(p) for p in e.get('loc', []))}: {e.get('msg', '')}"
                for e in errors[:10]
                if isinstance(e, dict)
            )
            msg += f" [{listed}]"
        if status == 401:
            msg += " - set STONKS_API_TOKEN for `stonks mcp` to the token `stonks serve` uses"
        return self.redact(msg)

"""A small synchronous client over the Stonks REST API for the scheduler.

Same rules as the MCP server's :class:`stonks.mcp.client.ApiClient` (the
pattern this follows): the bearer token is only sent over https or to a
loopback host, errors come back as :class:`SchedulerApiError` with the
token scrubbed, and the scheduler never touches the lake. The one addition
is ``trusted_hosts``: plain-http hosts the operator vouches for, such as
the ``api`` service on a private Compose network.

It is synchronous because the scheduler loop is; ``transport`` takes an
``httpx2.MockTransport`` in tests.
"""

from __future__ import annotations

import ipaddress
from collections.abc import Iterable
from typing import Any
from urllib.parse import urlsplit

import httpx2

_MAX_DETAIL = 300


class SchedulerApiError(Exception):
    def __init__(self, message: str, *, status: int | None = None) -> None:
        super().__init__(message)
        self.status = status


class SchedulerApiUnavailableError(SchedulerApiError):
    """The API could not be reached at all."""


def _is_loopback(host: str | None) -> bool:
    if not host:
        return False
    if host.lower() == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


class SchedulerApiClient:
    def __init__(
        self,
        base_url: str,
        *,
        token: str | None,
        timeout: float = 30.0,
        trusted_hosts: Iterable[str] = (),
        transport: httpx2.BaseTransport | None = None,
    ) -> None:
        parts = urlsplit(base_url)
        if parts.username or parts.password:
            raise ValueError("credentials in the API URL are not supported; use STONKS_API_TOKEN")
        trusted = {h.lower() for h in trusted_hosts}
        host = (parts.hostname or "").lower()
        if (
            token
            and parts.scheme != "https"
            and not _is_loopback(parts.hostname)
            and host not in trusted
        ):
            raise ValueError(
                f"refusing to send STONKS_API_TOKEN over plain http to {parts.hostname!r}; use "
                "https, a loopback URL, or list the host in [scheduler].api_trusted_hosts"
            )
        self.base_url = base_url.rstrip("/")
        self._token = token or None
        headers = {"Authorization": f"Bearer {token}"} if token else {}
        self._http = httpx2.Client(
            base_url=self.base_url, headers=headers, timeout=timeout, transport=transport
        )

    def __repr__(self) -> str:
        return f"SchedulerApiClient({self.base_url!r}, token={'***' if self._token else None})"

    def close(self) -> None:
        self._http.close()

    def redact(self, text: str) -> str:
        return text.replace(self._token, "***") if self._token else text

    def get(self, path: str, params: dict[str, Any] | None = None) -> Any:
        clean = {k: v for k, v in (params or {}).items() if v is not None}
        return self._request("GET", path, params=clean)

    def get_all(self, path: str, params: dict[str, Any] | None = None) -> list[Any]:
        """Every item of a paged list route (``{items, total, limit, offset}``),
        page after page."""
        items: list[Any] = []
        while True:
            page = self.get(path, {**(params or {}), "offset": len(items)})
            items.extend(page["items"])
            if not page["items"] or len(items) >= page["total"]:
                return items

    def post(self, path: str, body: dict[str, Any]) -> Any:
        if self._token is None:
            raise SchedulerApiError(
                "STONKS_API_TOKEN is not set: the scheduler starts jobs through the API "
                "and needs the token `stonks serve` was started with"
            )
        return self._request("POST", path, json=body)

    def _request(self, method: str, path: str, **kwargs: Any) -> Any:
        try:
            resp = self._http.request(method, path, **kwargs)
        except (httpx2.TransportError, httpx2.TimeoutException) as exc:
            raise SchedulerApiUnavailableError(
                f"Stonks API not reachable at {self.base_url} ({type(exc).__name__})"
            ) from None
        if resp.status_code >= 400:
            raise SchedulerApiError(self._error_message(resp), status=resp.status_code)
        return resp.json() if resp.content else None

    def _error_message(self, resp: httpx2.Response) -> str:
        try:
            body = resp.json()
        except ValueError:
            body = None
        if not isinstance(body, dict):
            return f"API error {resp.status_code}"
        msg = f"{body.get('title') or 'API error'} ({resp.status_code})"
        if body.get("detail"):
            msg += f": {str(body['detail'])[:_MAX_DETAIL]}"
        return self.redact(msg)

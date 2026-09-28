"""The HTTP side of a remote lab worker: ``/api/lab/worker/*`` (14.9).

Same rules as the scheduler's client (:mod:`stonks.scheduling.api_client`):
the token goes only over https, to a loopback host, or to a plain-http host
the operator lists (``STONKS_API_TRUSTED_HOSTS``, e.g. a Tailscale name on
a trusted network), and errors come back with the token scrubbed. ``http``
takes any ``httpx2.Client``, such as FastAPI's ``TestClient`` in tests.
"""

from __future__ import annotations

import ipaddress
from collections.abc import Iterable, Iterator
from contextlib import contextmanager
from typing import Any
from urllib.parse import urlsplit

import httpx2

#: The env var that holds the worker's ``lab_worker`` API token.
TOKEN_ENV = "STONKS_LAB_WORKER_TOKEN"
_PREFIX = "/api/lab/worker"
_MAX_DETAIL = 300


class LabWorkerApiError(Exception):
    def __init__(self, message: str, *, status: int | None = None) -> None:
        super().__init__(message)
        self.status = status


class LabWorkerApiUnavailable(LabWorkerApiError):
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


class LabWorkerApiClient:
    def __init__(
        self,
        base_url: str,
        *,
        token: str,
        timeout: float = 60.0,
        trusted_hosts: Iterable[str] = (),
        http: httpx2.Client | None = None,
    ) -> None:
        if not token:
            raise ValueError(f"a remote lab worker needs its API token in {TOKEN_ENV}")
        parts = urlsplit(base_url)
        if parts.username or parts.password:
            raise ValueError(f"credentials in the API URL are not supported; use {TOKEN_ENV}")
        if parts.scheme not in ("http", "https"):
            raise ValueError(f"the API URL must be http(s), got {base_url!r}")
        host = (parts.hostname or "").lower()
        trusted = {h.lower() for h in trusted_hosts}
        if parts.scheme != "https" and not _is_loopback(host) and host not in trusted:
            raise ValueError(
                f"refusing to send the lab worker token over plain http to {host!r}; use "
                "https, a loopback URL, or list the host in STONKS_API_TRUSTED_HOSTS"
            )
        self.base_url = base_url.rstrip("/")
        self._token = token
        self._auth = {"Authorization": f"Bearer {token}"}
        self._http = http or httpx2.Client(base_url=self.base_url, timeout=timeout)

    def __repr__(self) -> str:
        return f"LabWorkerApiClient({self.base_url!r}, token=***)"

    def close(self) -> None:
        self._http.close()

    def redact(self, text: str) -> str:
        return text.replace(self._token, "***")

    # ---- routes ------------------------------------------------------------------

    def register(self, worker_id: str, *, host: str, pid: int, cpus: int) -> dict[str, Any]:
        body = {"worker_id": worker_id, "host": host, "pid": pid, "cpus": cpus}
        return self._post("/register", body)

    def claim(self, worker_id: str) -> dict[str, Any] | None:
        return self._post("/claim", {"worker_id": worker_id}).get("job")

    def heartbeat(
        self, job_id: str, worker_id: str, *, progress: float, message: str | None
    ) -> dict[str, Any]:
        body = {"worker_id": worker_id, "progress": progress, "message": message}
        return self._post(f"/jobs/{job_id}/heartbeat", body)

    def complete(self, job_id: str, body: dict[str, Any]) -> dict[str, Any]:
        return self._post(f"/jobs/{job_id}/complete", body)

    def release(self, job_id: str, worker_id: str) -> dict[str, Any]:
        return self._post(f"/jobs/{job_id}/release", {"worker_id": worker_id})

    def stop(self, worker_id: str) -> dict[str, Any]:
        return self._post("/stop", {"worker_id": worker_id})

    @contextmanager
    def snapshot(self, name: str) -> Iterator[Iterator[bytes]]:
        """The tar bytes of snapshot ``name``, streamed."""
        try:
            with self._http.stream("GET", f"{_PREFIX}/snapshots/{name}", headers=self._auth) as r:
                if r.status_code >= 400:
                    r.read()
                    raise LabWorkerApiError(self._error_message(r), status=r.status_code)
                yield r.iter_bytes()
        except (httpx2.TransportError, httpx2.TimeoutException) as exc:
            raise LabWorkerApiUnavailable(
                f"Stonks API not reachable at {self.base_url} ({type(exc).__name__})"
            ) from None

    # ---- internals ---------------------------------------------------------------

    def _post(self, path: str, body: dict[str, Any]) -> Any:
        try:
            resp = self._http.request("POST", f"{_PREFIX}{path}", json=body, headers=self._auth)
        except (httpx2.TransportError, httpx2.TimeoutException) as exc:
            raise LabWorkerApiUnavailable(
                f"Stonks API not reachable at {self.base_url} ({type(exc).__name__})"
            ) from None
        if resp.status_code >= 400:
            raise LabWorkerApiError(self._error_message(resp), status=resp.status_code)
        return resp.json() if resp.content else {}

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

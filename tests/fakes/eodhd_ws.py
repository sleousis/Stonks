"""``FakeEodhdServer``: a scripted EODHD websocket server on loopback
(roadmap 21.1, ``docs/design/intraday.md`` section 7).

It speaks the vendor's protocol: the client connects to ``/ws/<feed>?
api_token=KEY``, the server answers ``{"status_code": 200, "message":
"Authorized"}`` (or 401 for a wrong key and closes), the client sends
``{"action": "subscribe", "symbols": "AAPL,MSFT"}``, then the server sends
the scripted messages of that feed.

Scriptable per test: the messages per feed, dropping the connection after
the script (``drop_after_script``), or after N messages on the first
connection only (``drop_first_after``, the next connection resumes there),
and a wrong key. Sockets bind to
127.0.0.1 only, which the hermetic test run allows.
"""

from __future__ import annotations

import json
import threading
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

from websockets.exceptions import ConnectionClosed
from websockets.sync.server import Server, ServerConnection, serve

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "streaming"


def load_messages(name: str) -> list[dict[str, Any]]:
    """The recorded messages in ``tests/fixtures/streaming/<name>.jsonl``."""
    lines = (FIXTURES / f"{name}.jsonl").read_text(encoding="utf-8").splitlines()
    return [json.loads(line) for line in lines if line.strip()]


class FakeEodhdServer:
    def __init__(
        self,
        script: Mapping[str, Sequence[Mapping[str, Any]]],
        *,
        api_key: str = "test-key",
        drop_after_script: bool = False,
        drop_first_after: int | None = None,
    ) -> None:
        self.script = {feed: list(msgs) for feed, msgs in script.items()}
        self.api_key = api_key
        self.drop_after_script = drop_after_script
        self.drop_first_after = drop_first_after
        self.connects: list[str] = []
        self.subscriptions: list[tuple[str, list[str]]] = []
        self.rejected = 0
        self._lock = threading.Lock()
        self._server: Server | None = None
        self._thread: threading.Thread | None = None

    # ---- lifecycle ------------------------------------------------------------------

    def __enter__(self) -> FakeEodhdServer:
        self._server = serve(self._handle, "127.0.0.1", 0, compression=None)
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)
        self._thread.start()
        return self

    def __exit__(self, *exc: object) -> None:
        if self._server is not None:
            self._server.shutdown()
        if self._thread is not None:
            self._thread.join(timeout=5)

    @property
    def url(self) -> str:
        assert self._server is not None
        port = self._server.socket.getsockname()[1]
        return f"ws://127.0.0.1:{port}/ws"

    # ---- protocol -------------------------------------------------------------------

    def _handle(self, ws: ServerConnection) -> None:
        request = ws.request
        assert request is not None
        parsed = urlparse(request.path)
        feed = parsed.path.rsplit("/", 1)[-1]
        token = parse_qs(parsed.query).get("api_token", [""])[0]
        with self._lock:
            self.connects.append(feed)
            first = self.connects.count(feed) == 1
        try:
            if token != self.api_key:
                with self._lock:
                    self.rejected += 1
                ws.send(json.dumps({"status_code": 401, "message": "Unauthorized"}))
                ws.close(code=1008)
                return
            ws.send(json.dumps({"status_code": 200, "message": "Authorized"}))
            sub = json.loads(ws.recv(timeout=5))
            symbols = [s.strip() for s in str(sub.get("symbols", "")).split(",") if s.strip()]
            with self._lock:
                self.subscriptions.append((feed, symbols))
            script = self.script.get(feed, [])
            # a later connection resumes where the dropped one stopped
            begin = 0 if first or self.drop_first_after is None else self.drop_first_after
            for i, msg in enumerate(script[begin:], start=begin):
                if first and self.drop_first_after is not None and i >= self.drop_first_after:
                    ws.close(code=1011)
                    return
                if msg.get("s") in symbols:
                    ws.send(json.dumps(msg))
            if self.drop_after_script:
                ws.close(code=1011)
                return
            # keep the connection open until the client leaves
            while True:
                ws.recv()
        except ConnectionClosed:
            return

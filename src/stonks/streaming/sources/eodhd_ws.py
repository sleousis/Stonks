"""The ``eodhd`` streaming source: EODHD's websocket feeds (roadmap 21.1).

EODHD serves one websocket per feed at ``<url>/<feed>?api_token=KEY``:
``us`` (US trades), ``us-quote`` (US bid and ask), ``crypto`` and
``forex``. After the connection the server answers ``{"status_code":
200}`` (or 401 for a bad key), the client sends ``{"action": "subscribe",
"symbols": "AAPL,MSFT"}``, and messages follow:

- ``us``: ``{"s", "p" price, "v" size, "c" conditions, "dp" dark pool,
  "ms" market status, "t" epoch ms}``
- ``us-quote``: ``{"s", "ap" ask, "as" ask size, "bp" bid, "bs" bid size, "t"}``
- ``crypto``: ``{"s", "p", "q" size (both strings), "dc", "dd", "t"}``
- ``forex``: ``{"s", "a" ask, "b" bid, "dc", "dd", "ppms", "t"}``

A ticker's suffix picks its feed (``[streaming.eodhd] feeds``: ``AAPL.US``
goes to ``us`` as ``AAPL``). Each feed runs on its own connection and
reader thread, and the events of all feeds come out of one iterator.
Conditions, dark pool flags and market status are not kept: they have no
vendor-neutral column, and session rules come from the calendars.

``websockets`` is imported only here. The key never appears in a log or
an error: every error text is scrubbed before it leaves this module.
"""

from __future__ import annotations

import contextlib
import json
import math
import os
import queue
import threading
from collections.abc import Iterator, Mapping, Sequence
from datetime import UTC, datetime
from typing import Any, Literal, Self, cast

from websockets.exceptions import ConnectionClosed, InvalidStatus, WebSocketException
from websockets.sync.client import ClientConnection, connect

from stonks.config import secret_value
from stonks.core.clock import SYSTEM_CLOCK, Clock
from stonks.core.stream import DataEvent, Heartbeat, QuoteTick, StreamEvent, TradeTick
from stonks.logging import get_logger
from stonks.streaming.base import (
    StreamAuthError,
    StreamConfigError,
    StreamContext,
    StreamDisconnectedError,
    StreamError,
    StreamingSource,
)
from stonks.streaming.registry import register_stream_source

FeedKind = Literal["trades", "quotes", "crypto", "forex"]

SOURCE = "eodhd"
_POLL_SECONDS = 0.25
_log = get_logger("stonks.streaming.eodhd")


def feed_kind(feed: str) -> FeedKind:
    if feed.endswith("-quote"):
        return "quotes"
    if feed in ("crypto", "forex"):
        return cast("FeedKind", feed)
    return "trades"


def _num(value: Any) -> float | None:
    try:
        out = float(value)
    except (TypeError, ValueError):
        return None
    return out if math.isfinite(out) else None


def _time(msg: Mapping[str, Any]) -> datetime | None:
    ms = _num(msg.get("t"))
    if ms is None or ms <= 0:
        return None
    return datetime.fromtimestamp(ms / 1000.0, UTC)


def parse_message(kind: FeedKind, msg: Mapping[str, Any], ticker: str) -> DataEvent | None:
    """One vendor message as an event, ``None`` when it is not usable."""
    at = _time(msg)
    if at is None:
        return None
    if kind in ("trades", "crypto"):
        price = _num(msg.get("p"))
        size = _num(msg.get("v" if kind == "trades" else "q"))
        if price is None or price <= 0:
            return None
        return TradeTick(ticker, at, price, max(size or 0.0, 0.0), SOURCE)
    bid_key, ask_key = ("bp", "ap") if kind == "quotes" else ("b", "a")
    bid, ask = _num(msg.get(bid_key)), _num(msg.get(ask_key))
    if bid is None and ask is None:
        return None
    return QuoteTick(
        ticker,
        at,
        bid=bid,
        ask=ask,
        bid_size=_num(msg.get("bs")) if kind == "quotes" else None,
        ask_size=_num(msg.get("as")) if kind == "quotes" else None,
        source=SOURCE,
    )


@register_stream_source("eodhd")
class EodhdStreamSource(StreamingSource):
    def __init__(
        self,
        api_key: str,
        *,
        url: str = "wss://ws.eodhistoricaldata.com/ws",
        feeds: Mapping[str, str] | None = None,
        quotes: bool = False,
        heartbeat_seconds: float = 5.0,
        open_timeout: float = 10.0,
        clock: Clock = SYSTEM_CLOCK,
    ) -> None:
        if not api_key:
            raise StreamConfigError("the EODHD stream needs EODHD_API_KEY")
        self._key = api_key
        self.url = url.rstrip("/")
        self.feeds = dict(feeds or {"US": "us", "CC": "crypto", "FOREX": "forex"})
        self.quotes = quotes
        self.heartbeat_seconds = heartbeat_seconds
        self.open_timeout = open_timeout
        self.clock = clock
        self.bad_messages = 0
        self._stop = threading.Event()
        self._queue: queue.Queue[DataEvent | BaseException | None] | None = None

    def __repr__(self) -> str:
        return f"EodhdStreamSource(url={self.url!r}, feeds={self.feeds!r}, quotes={self.quotes})"

    @classmethod
    def from_settings(cls, ctx: StreamContext) -> Self:
        s = ctx.settings
        key = secret_value(s.sources.eodhd.api_key) or os.environ.get("EODHD_API_KEY")
        if not key:
            raise StreamConfigError(
                "EODHD_API_KEY is not set (add it to .env or your shell environment)"
            )
        cfg = s.streaming.eodhd
        return cls(
            key,
            url=cfg.url,
            feeds=cfg.feeds,
            quotes=cfg.quotes,
            heartbeat_seconds=s.streaming.heartbeat_seconds,
            open_timeout=cfg.open_timeout_seconds,
            clock=ctx.clock,
        )

    # ---- tickers --------------------------------------------------------------------

    def _split(self, ticker: str) -> tuple[str, str] | None:
        symbol, dot, suffix = ticker.rpartition(".")
        if not dot or not symbol:
            return None
        feed = self.feeds.get(suffix.upper())
        return (feed, symbol) if feed else None

    def supports(self, ticker: str) -> bool:
        return self._split(ticker) is not None

    def plan(self, tickers: Sequence[str]) -> dict[str, dict[str, str]]:
        """Feed to ``{vendor symbol: ticker}``. Unsupported tickers are left out."""
        out: dict[str, dict[str, str]] = {}
        for ticker in tickers:
            split = self._split(ticker)
            if split is None:
                _log.warning("stream.eodhd.unsupported", ticker=ticker)
                continue
            feed, symbol = split
            out.setdefault(feed, {})[symbol] = ticker
            if self.quotes and feed_kind(feed) == "trades":
                out.setdefault(f"{feed}-quote", {})[symbol] = ticker
        return out

    # ---- streaming ------------------------------------------------------------------

    def _scrub(self, text: str) -> str:
        return text.replace(self._key, "***")

    def _open(
        self, feed: str, symbols: Sequence[str], stack: contextlib.ExitStack
    ) -> ClientConnection:
        where = f"{self.url}/{feed}"
        try:
            ws = stack.enter_context(
                connect(f"{where}?api_token={self._key}", open_timeout=self.open_timeout)
            )
        except InvalidStatus as exc:
            status = exc.response.status_code
            if status in (401, 403):
                raise StreamAuthError(f"EODHD refused the key on {where} ({status})") from None
            raise StreamDisconnectedError(f"EODHD {where} answered {status}") from None
        except (OSError, TimeoutError, WebSocketException) as exc:
            raise StreamDisconnectedError(
                f"cannot open {where}: {self._scrub(str(exc)) or type(exc).__name__}"
            ) from None
        try:
            first = json.loads(ws.recv(timeout=self.open_timeout))
            if isinstance(first, dict):
                status = cast("dict[str, Any]", first).get("status_code")
                if status in (401, 403):
                    raise StreamAuthError(f"EODHD refused the key on {where} ({status})")
            ws.send(json.dumps({"action": "subscribe", "symbols": ",".join(symbols)}))
        except StreamError:
            raise
        except (OSError, TimeoutError, ValueError, ConnectionClosed) as exc:
            raise StreamDisconnectedError(
                f"EODHD {where} failed at login: {self._scrub(str(exc)) or type(exc).__name__}"
            ) from None
        _log.info("stream.eodhd.subscribed", feed=feed, symbols=len(symbols))
        return ws

    def _pump(
        self,
        feed: str,
        ws: ClientConnection,
        symbols: Mapping[str, str],
        out: queue.Queue[DataEvent | BaseException | None],
        stop: threading.Event,
    ) -> None:
        kind = feed_kind(feed)
        while not stop.is_set():
            try:
                raw = ws.recv(timeout=_POLL_SECONDS)
            except TimeoutError:
                continue
            except ConnectionClosed as exc:
                if not stop.is_set():
                    code = exc.rcvd.code if exc.rcvd is not None else None
                    out.put(StreamDisconnectedError(f"EODHD {feed} feed closed (code {code})"))
                return
            except Exception as exc:  # any reader failure ends this feed
                if not stop.is_set():
                    out.put(StreamDisconnectedError(f"EODHD {feed}: {self._scrub(str(exc))}"))
                return
            self._handle(kind, feed, raw, symbols, out)

    def _handle(
        self,
        kind: FeedKind,
        feed: str,
        raw: str | bytes,
        symbols: Mapping[str, str],
        out: queue.Queue[DataEvent | BaseException | None],
    ) -> None:
        try:
            decoded: Any = json.loads(raw)
        except ValueError:
            self.bad_messages += 1
            return
        items: list[Any] = cast("list[Any]", decoded) if isinstance(decoded, list) else [decoded]
        for item in items:
            if not isinstance(item, dict):
                self.bad_messages += 1
                continue
            msg = cast("dict[str, Any]", item)
            if "status_code" in msg:
                if msg.get("status_code") in (401, 403):
                    out.put(StreamAuthError(f"EODHD refused the key on the {feed} feed"))
                continue
            ticker = symbols.get(str(msg.get("s", "")))
            event = parse_message(kind, msg, ticker) if ticker else None
            if event is None:
                self.bad_messages += 1
                continue
            out.put(event)

    def stream(self, tickers: Sequence[str]) -> Iterator[StreamEvent]:
        plan = self.plan(tickers)
        if not plan:
            raise StreamConfigError("none of the tickers can stream from EODHD")
        self._stop.clear()
        stop = threading.Event()
        out: queue.Queue[DataEvent | BaseException | None] = queue.Queue()
        self._queue = out
        threads: list[threading.Thread] = []
        stack = contextlib.ExitStack()
        try:
            for feed, symbols in plan.items():
                ws = self._open(feed, list(symbols), stack)
                t = threading.Thread(
                    target=self._pump,
                    args=(feed, ws, symbols, out, stop),
                    name=f"stream-eodhd-{feed}",
                    daemon=True,
                )
                threads.append(t)
            for t in threads:
                t.start()
            while not self._stop.is_set():
                try:
                    item = out.get(timeout=self.heartbeat_seconds)
                except queue.Empty:
                    yield Heartbeat(self.clock.now(), SOURCE)
                    continue
                if item is None:
                    continue
                if isinstance(item, BaseException):
                    raise item
                yield item
        finally:
            stop.set()
            stack.close()
            for t in threads:
                t.join(timeout=2.0)
            self._queue = None

    def close(self) -> None:
        self._stop.set()
        pending = self._queue
        if pending is not None:
            pending.put(None)

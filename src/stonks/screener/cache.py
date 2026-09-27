"""A short-lived, in-process cache of screen results (roadmap 20.11).

The key is the spec (as canonical JSON, so field order and defaults do not
matter) plus the as-of date. A result is reused for ``ttl_seconds``, then
read again, so a fresh ingest shows up within that time. Each result was
computed with only the data known on its date (P12), and the cache never
changes the date a result belongs to.
"""

from __future__ import annotations

import json
import threading
import time
from collections import OrderedDict
from collections.abc import Callable
from datetime import date

from stonks.screener.engine import ScreenResult
from stonks.screener.spec import ScreenSpec

__all__ = ["ScreenCache", "cache_key"]


def cache_key(spec: ScreenSpec, as_of: date) -> str:
    body = spec.model_dump(mode="json")
    return json.dumps({"spec": body, "as_of": as_of.isoformat()}, sort_keys=True)


class ScreenCache:
    """Thread safe. The oldest entry goes when ``max_entries`` is reached
    (a hit counts as use). ``ttl_seconds`` of 0 keeps nothing."""

    def __init__(
        self,
        ttl_seconds: float,
        max_entries: int,
        *,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._ttl = ttl_seconds
        self._max = max_entries
        self._clock = clock
        self._items: OrderedDict[str, tuple[float, ScreenResult]] = OrderedDict()
        self._lock = threading.Lock()

    def __len__(self) -> int:
        with self._lock:
            self._expire()
            return len(self._items)

    def get(self, spec: ScreenSpec, as_of: date) -> ScreenResult | None:
        key = cache_key(spec, as_of)
        with self._lock:
            self._expire()
            item = self._items.get(key)
            if item is None:
                return None
            self._items.move_to_end(key)
            return item[1]

    def put(self, spec: ScreenSpec, as_of: date, result: ScreenResult) -> None:
        if self._ttl <= 0:
            return
        key = cache_key(spec, as_of)
        with self._lock:
            self._items[key] = (self._clock() + self._ttl, result)
            self._items.move_to_end(key)
            while len(self._items) > self._max:
                self._items.popitem(last=False)

    def clear(self) -> None:
        with self._lock:
            self._items.clear()

    def _expire(self) -> None:
        now = self._clock()
        for key in [k for k, (expires, _) in self._items.items() if expires <= now]:
            del self._items[key]

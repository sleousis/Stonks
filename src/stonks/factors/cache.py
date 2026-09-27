"""The factor panel cache (roadmap 22.2).

A panel is keyed by a :class:`PanelKey`: a *slot* (the factor, universe,
window, interval and sampling) and a *fingerprint* of the data it read
(bar checksums, corporate actions, membership spans, and any other table
the factor declares). A slot holds one panel. When the data changes the
fingerprint changes, the next read misses, and the stale panel is dropped,
so a cache hit is always the panel today's data would give.

Stores behind :class:`PanelCache`: :class:`MemoryPanelCache` (a bounded
LRU for one process) and :class:`ParquetPanelCache` (one Parquet file per
slot under a folder, safe to delete at any time).
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import os
import threading
import uuid
from abc import ABC, abstractmethod
from collections import OrderedDict
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import duckdb
import pandas as pd

from stonks.logging import get_logger
from stonks.store.pit import PointInTimeLake

__all__ = [
    "MemoryPanelCache",
    "PanelCache",
    "PanelKey",
    "ParquetPanelCache",
    "data_fingerprint",
    "slot_of",
]

_log = get_logger("stonks.factors.cache")


def _digest(payload: Any) -> str:
    text = json.dumps(payload, sort_keys=True, default=str, separators=(",", ":"))
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:32]


@dataclass(frozen=True)
class PanelKey:
    slot: str
    fingerprint: str


def slot_of(
    token: str,
    universe: Sequence[str],
    window: tuple[Any, Any],
    interval: str,
    extra: Any = None,
) -> str:
    """The slot of a panel: everything that defines it except the data."""
    return _digest([token, list(universe), [str(window[0]), str(window[1])], interval, extra])


def data_fingerprint(
    lake: Any,
    tickers: Sequence[str],
    interval: str,
    start: Any,
    end: Any,
    *,
    tables: Sequence[str] = (),
    membership: pd.DataFrame | None = None,
) -> str | None:
    """A digest of the rows a panel reads, or ``None`` when ``lake`` cannot
    run the checksum queries (a point-in-time view, a test double): then the
    panel is not cached. ``tables`` are extra lake tables with a ``ticker``
    column the factor reads (statements, share counts)."""
    sql = getattr(lake, "sql", None)
    if isinstance(lake, PointInTimeLake) or not callable(sql):
        return None
    tickers = list(tickers)
    try:
        bars = sql(
            "SELECT COUNT(*) AS n, COALESCE(SUM(hash(ticker, timestamp, open, high, low, "
            "close, adj_close, volume)), 0) AS h FROM bars WHERE ticker = ANY(?) "
            "AND interval = ? AND timestamp BETWEEN ? AND ?",
            [tickers, interval, start, end],
        )
        parts: list[Any] = [[int(bars["n"].iloc[0]), str(bars["h"].iloc[0])]]
        actions = getattr(lake, "get_corporate_actions", None)
        if callable(actions):
            frame = actions(tickers)
            parts.append(_frame_digest(frame))
        for table in tables:
            row = sql(
                f"SELECT COUNT(*) AS n, COALESCE(SUM(hash(t)), 0) AS h FROM {table} t "
                "WHERE ticker = ANY(?)",
                [tickers],
            )
            parts.append([table, int(row["n"].iloc[0]), str(row["h"].iloc[0])])
    except Exception as exc:  # a missing table or a lake without bars
        _log.warning("factor.cache.fingerprint_failed", error=str(exc))
        return None
    if membership is not None:
        parts.append(_frame_digest(membership))
    return _digest(parts)


def _frame_digest(frame: pd.DataFrame | None) -> str:
    if frame is None or frame.empty:
        return "empty"
    hashed = pd.util.hash_pandas_object(frame.astype(str), index=False)
    return hashlib.sha256(hashed.to_numpy().tobytes()).hexdigest()[:32]


class PanelCache(ABC):
    """Where panels are kept between evaluations."""

    def __init__(self) -> None:
        self.hits = 0
        self.misses = 0

    def get(self, key: PanelKey) -> pd.DataFrame | None:
        """The panel for ``key``, or ``None`` (a stale panel is dropped)."""
        got = self._get(key)
        if got is None:
            self.misses += 1
        else:
            self.hits += 1
        return got

    def put(self, key: PanelKey, panel: pd.DataFrame) -> None:
        """Keep ``panel`` as the slot's only panel."""
        self._put(key, panel)

    @abstractmethod
    def _get(self, key: PanelKey) -> pd.DataFrame | None: ...

    @abstractmethod
    def _put(self, key: PanelKey, panel: pd.DataFrame) -> None: ...


class MemoryPanelCache(PanelCache):
    """A least-recently-used cache of at most ``max_panels`` panels."""

    def __init__(self, max_panels: int = 64) -> None:
        super().__init__()
        self.max_panels = max_panels
        self._items: OrderedDict[str, tuple[str, pd.DataFrame]] = OrderedDict()
        self._lock = threading.Lock()

    def _get(self, key: PanelKey) -> pd.DataFrame | None:
        with self._lock:
            item = self._items.get(key.slot)
            if item is None:
                return None
            if item[0] != key.fingerprint:
                del self._items[key.slot]  # the data changed: invalidate
                return None
            self._items.move_to_end(key.slot)
            return item[1].copy()

    def _put(self, key: PanelKey, panel: pd.DataFrame) -> None:
        with self._lock:
            self._items[key.slot] = (key.fingerprint, panel.copy())
            self._items.move_to_end(key.slot)
            while len(self._items) > self.max_panels:
                self._items.popitem(last=False)

    def __len__(self) -> int:
        return len(self._items)


class ParquetPanelCache(PanelCache):
    """Panels as ``<root>/<slot>.<fingerprint>.parquet``, one per slot."""

    def __init__(self, root: str | Path) -> None:
        super().__init__()
        self.root = Path(root)

    def _files(self, slot: str) -> list[Path]:
        if not self.root.exists():
            return []
        return sorted(self.root.glob(f"{slot}.*.parquet"))

    def _get(self, key: PanelKey) -> pd.DataFrame | None:
        wanted = self.root / f"{key.slot}.{key.fingerprint}.parquet"
        for path in self._files(key.slot):
            if path != wanted:
                _unlink(path)  # the data changed: invalidate
        if not wanted.exists():
            return None
        try:
            frame = _read_parquet(wanted)
        except Exception as exc:  # a torn or foreign file: treat as a miss
            _log.warning("factor.cache.read_failed", path=str(wanted), error=str(exc))
            _unlink(wanted)
            return None
        return frame

    def _put(self, key: PanelKey, panel: pd.DataFrame) -> None:
        self.root.mkdir(parents=True, exist_ok=True)
        target = self.root / f"{key.slot}.{key.fingerprint}.parquet"
        tmp = self.root / f".{uuid.uuid4().hex}.tmp"
        _write_parquet(panel, tmp)
        os.replace(tmp, target)
        for path in self._files(key.slot):
            if path != target:
                _unlink(path)

    def __len__(self) -> int:
        return len(list(self.root.glob("*.parquet"))) if self.root.exists() else 0


_INDEX = "__timestamp__"


def _write_parquet(panel: pd.DataFrame, path: Path) -> None:
    """``panel`` (a timestamp index, ticker columns) as Parquet, via DuckDB."""
    frame = panel.copy()
    frame.columns = [str(c) for c in frame.columns]
    frame.insert(0, _INDEX, pd.DatetimeIndex(panel.index))
    frame = frame.reset_index(drop=True)
    con = duckdb.connect()
    try:
        con.register("panel", frame)
        target = str(path).replace("'", "''")
        con.execute(f"COPY panel TO '{target}' (FORMAT PARQUET)")
    finally:
        con.close()


def _read_parquet(path: Path) -> pd.DataFrame:
    con = duckdb.connect()
    try:
        frame = con.execute("SELECT * FROM read_parquet(?)", [str(path)]).fetchdf()
    finally:
        con.close()
    index = pd.DatetimeIndex(pd.to_datetime(frame.pop(_INDEX)), name="timestamp")
    frame.index = index
    return frame.astype(float)


def _unlink(path: Path) -> None:
    with contextlib.suppress(OSError):  # another process removed it first
        path.unlink()

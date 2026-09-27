"""Record a stream to Parquet and read it back (roadmap 21.1).

A recording is what makes streams testable and replayable: the
:class:`~stonks.streaming.sources.replay.ReplaySource` plays one back
through the same :class:`~stonks.streaming.base.StreamingSource`
interface, so hermetic tests and the shared replay driver of 21.2 see
exactly what a live feed said.

Layout: one Parquet file per chunk, under the UTC day of its first event::

    <root>/2026-09-28/part-20260928T133000123000-1a2b3c4d.parquet

Every event kind goes in one table, heartbeats included (they carry the
passage of time a replay needs to close bars on schedule). Columns that do
not apply to a kind are NULL. Timestamps are naive UTC. A chunk is written
to a temp file and renamed, so a reader never sees half a file. DuckDB
writes and reads the files, so no other Parquet library is needed.
"""

from __future__ import annotations

import os
import uuid
from collections.abc import Iterator, Sequence
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import TracebackType
from typing import Any, Self

import duckdb
import pandas as pd

from stonks.core.clock import SYSTEM_CLOCK, Clock
from stonks.core.interval import Interval
from stonks.core.stream import Heartbeat, QuoteTick, StreamBar, StreamEvent, TradeTick

#: The columns of a recording, with their Parquet (DuckDB) types.
RECORD_COLUMNS: tuple[tuple[str, str], ...] = (
    ("seq", "BIGINT"),
    ("kind", "VARCHAR"),
    ("ticker", "VARCHAR"),
    ("timestamp", "TIMESTAMP"),
    ("price", "DOUBLE"),
    ("size", "DOUBLE"),
    ("bid", "DOUBLE"),
    ("ask", "DOUBLE"),
    ("bid_size", "DOUBLE"),
    ("ask_size", "DOUBLE"),
    ("last", "DOUBLE"),
    ("delayed", "BOOLEAN"),
    ("interval", "VARCHAR"),
    ("open", "DOUBLE"),
    ("high", "DOUBLE"),
    ("low", "DOUBLE"),
    ("close", "DOUBLE"),
    ("volume", "BIGINT"),
    ("source", "VARCHAR"),
)
_NAMES = tuple(name for name, _ in RECORD_COLUMNS)
_TYPED = ", ".join(f'CAST("{n}" AS {t}) AS "{n}"' for n, t in RECORD_COLUMNS)
_READ_BATCH = 10_000


def _naive(when: datetime) -> datetime:
    return when.astimezone(UTC).replace(tzinfo=None)


def event_row(event: StreamEvent, seq: int) -> dict[str, Any]:
    """One event as a recording row."""
    row: dict[str, Any] = dict.fromkeys(_NAMES)
    row.update(seq=seq, kind=event.kind, timestamp=_naive(event.timestamp), source=event.source)
    if isinstance(event, TradeTick):
        row.update(ticker=event.ticker, price=event.price, size=event.size)
    elif isinstance(event, QuoteTick):
        row.update(
            ticker=event.ticker,
            bid=event.bid,
            ask=event.ask,
            bid_size=event.bid_size,
            ask_size=event.ask_size,
            last=event.last,
            delayed=event.delayed,
        )
    elif isinstance(event, StreamBar):
        row.update(
            ticker=event.ticker,
            interval=event.interval.code,
            open=event.open,
            high=event.high,
            low=event.low,
            close=event.close,
            volume=event.volume,
        )
    return row


def _opt(value: Any) -> float | None:
    return None if value is None or pd.isna(value) else float(value)


def _as_utc(value: Any) -> datetime:
    when = value if isinstance(value, datetime) else pd.Timestamp(value).to_pydatetime()
    if not isinstance(when, datetime):
        raise ValueError(f"a recording row has no timestamp: {value!r}")
    return when.replace(tzinfo=UTC)


def event_from_row(row: dict[str, Any]) -> StreamEvent:
    """A recording row back as its event."""
    kind = row["kind"]
    ts = _as_utc(row["timestamp"])
    source = row.get("source") or ""
    if kind == "trade":
        return TradeTick(row["ticker"], ts, float(row["price"]), _opt(row["size"]) or 0.0, source)
    if kind == "quote":
        return QuoteTick(
            row["ticker"],
            ts,
            bid=_opt(row["bid"]),
            ask=_opt(row["ask"]),
            bid_size=_opt(row["bid_size"]),
            ask_size=_opt(row["ask_size"]),
            last=_opt(row["last"]),
            delayed=bool(row["delayed"]),
            source=source,
        )
    if kind == "bar":
        return StreamBar(
            row["ticker"],
            ts,
            Interval.parse(row["interval"]),
            float(row["open"]),
            float(row["high"]),
            float(row["low"]),
            float(row["close"]),
            int(row["volume"] or 0),
            source,
        )
    if kind == "heartbeat":
        return Heartbeat(ts, source)
    raise ValueError(f"unknown event kind {kind!r} in a recording")


class StreamRecorder:
    """Appends events and writes a Parquet chunk every ``chunk_events``
    events or ``chunk_every`` of clock time."""

    def __init__(
        self,
        root: str | Path,
        *,
        chunk_events: int = 5000,
        chunk_every: timedelta = timedelta(seconds=60),
        clock: Clock = SYSTEM_CLOCK,
    ) -> None:
        self.root = Path(root)
        self.chunk_events = max(1, chunk_events)
        self.chunk_every = chunk_every
        self.clock = clock
        self._rows: list[dict[str, Any]] = []
        self._chunk_started: datetime | None = None
        self._seq = 0
        self.events_written = 0
        self.files: list[Path] = []

    def write(self, event: StreamEvent) -> None:
        now = self.clock.now()
        if self._chunk_started is None:
            self._chunk_started = now
        self._rows.append(event_row(event, self._seq))
        self._seq += 1
        if len(self._rows) >= self.chunk_events or now - self._chunk_started >= self.chunk_every:
            self.flush()

    def flush(self) -> Path | None:
        """Write the pending events as one chunk. Returns its path."""
        if not self._rows:
            return None
        rows, self._rows = self._rows, []
        self._chunk_started = None
        first = min(r["timestamp"] for r in rows)
        folder = self.root / first.strftime("%Y-%m-%d")
        folder.mkdir(parents=True, exist_ok=True)
        target = folder / f"part-{first.strftime('%Y%m%dT%H%M%S%f')}-{uuid.uuid4().hex[:8]}.parquet"
        tmp = target.with_suffix(".tmp")
        frame = pd.DataFrame(rows, columns=list(_NAMES))
        con = duckdb.connect()
        try:
            con.execute("SET TimeZone = 'UTC'")
            con.register("events", frame)
            con.execute(
                f"COPY (SELECT {_TYPED} FROM events) TO '{_sql_path(tmp)}' (FORMAT PARQUET)"
            )
        finally:
            con.close()
        os.replace(tmp, target)
        self.files.append(target)
        self.events_written += len(rows)
        return target

    def close(self) -> None:
        self.flush()

    def __enter__(self) -> Self:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        self.close()


def _sql_path(path: Path) -> str:
    return path.as_posix().replace("'", "''")


def read_recording(
    root: str | Path,
    *,
    tickers: Sequence[str] | None = None,
    start: datetime | None = None,
    end: datetime | None = None,
) -> Iterator[StreamEvent]:
    """Every event under ``root`` in time order (ties keep file and write
    order). ``tickers`` keeps those tickers and every heartbeat. ``start``
    and ``end`` bound the timestamps, both inclusive."""
    base = Path(root)
    if not base.exists() or not any(base.rglob("*.parquet")):
        return
    where: list[str] = []
    params: list[Any] = []
    if tickers:
        where.append(f"(kind = 'heartbeat' OR ticker IN ({', '.join('?' for _ in tickers)}))")
        params.extend(tickers)
    if start is not None:
        where.append("timestamp >= ?")
        params.append(_naive(start))
    if end is not None:
        where.append("timestamp <= ?")
        params.append(_naive(end))
    clause = f"WHERE {' AND '.join(where)}" if where else ""
    glob = _sql_path(base) + "/**/*.parquet"
    con = duckdb.connect()
    try:
        con.execute("SET TimeZone = 'UTC'")
        cur = con.execute(
            f"""
            SELECT {", ".join(f'"{n}"' for n in _NAMES)}
              FROM read_parquet('{glob}', filename = true, union_by_name = true)
              {clause}
             ORDER BY timestamp, filename, seq
            """,
            params,
        )
        while True:
            batch = cur.fetchmany(_READ_BATCH)
            if not batch:
                break
            for values in batch:
                yield event_from_row(dict(zip(_NAMES, values, strict=True)))
    finally:
        con.close()

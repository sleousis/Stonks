"""Where the lake keeps its OHLCV bars: the :class:`BarStore` seam.

Two stores implement it:

- :class:`DuckDBTableBarStore` — the ``bars`` table inside the lake's
  DuckDB file (migration 003). The default, and what every test lake uses.
- :class:`ParquetBarStore` — hive-partitioned Parquet files next to the
  lake file::

      <lake dir>/bars/interval=<code>/ticker=<id>/year=<yyyy>/part-0.parquet

  DuckDB lets one process at a time open a database file for writing, so
  while ``stonks serve`` holds the lake no other process can read the
  table. Parquet files can be read by any number of processes while one
  writer updates them.

Parquet layout and write protocol:

- A partition is one ``(interval, ticker, year)`` directory holding one
  file, ``part-0.parquet``. The files carry ``timestamp`` and the value
  columns; ``interval``, ``ticker`` and ``year`` come from the path.
  Path values are percent-encoded (DuckDB decodes them on read), so
  tickers such as ``ES=F`` or ``^GSPC`` are safe. Two tickers that differ
  only by case are refused: they would share a directory on Windows and
  macOS.
- An upsert rewrites each affected partition: read the existing file,
  merge the new rows (last write wins per ``timestamp``), write a temp
  file (``*.tmp``, which no reader glob matches) in the same directory and
  ``os.replace`` it over ``part-0.parquet``. Readers therefore see either
  the old or the new file, never a partial one, and never both. Any other
  ``*.parquet`` file in the partition is folded in and removed
  (compaction), and leftover temp files of a crashed writer are deleted.
- Writers take an inter-process lock per ``(interval, ticker)`` series
  (``bars/_locks/...``) for the whole read-merge-replace, so two writers
  never interleave on a partition. Readers take no lock.
- On Windows ``os.replace`` fails while a reader has the target open; the
  writer retries until ``lock_timeout``. Readers never wait.
- A multi-year upsert is atomic per partition, not across partitions.
- Timestamps are stored as naive UTC ``TIMESTAMP``, like the table: the
  connection runs with ``TimeZone = 'UTC'`` and tz-aware input is
  converted to UTC before the time zone is dropped.
- An empty, zero-row sentinel partition (``interval=_/ticker=_/year=0``)
  keeps the reader glob non-empty, so the ``bars`` view works on an empty
  store.
"""

from __future__ import annotations

import contextlib
import os
import shutil
import time
import uuid
from abc import ABC, abstractmethod
from collections.abc import Iterable, Iterator
from contextlib import contextmanager
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING, Any, ClassVar, Literal
from urllib.parse import quote, unquote

import duckdb
import numpy as np
import pandas as pd

from stonks.core.interval import Interval
from stonks.store.filelock import FileLock

if TYPE_CHECKING:
    from stonks.store.lake import DuckDBLake

BarBackend = Literal["duckdb", "parquet"]

BAR_COLUMNS: tuple[str, ...] = (
    "ticker",
    "timestamp",
    "interval",
    "open",
    "high",
    "low",
    "close",
    "adj_close",
    "volume",
)
BAR_KEY: tuple[str, ...] = ("ticker", "timestamp", "interval")

# Columns inside a partition file, with the table's declared types.
_FILE_TYPES: tuple[tuple[str, str], ...] = (
    ("timestamp", "TIMESTAMP"),
    ("open", "DOUBLE"),
    ("high", "DOUBLE"),
    ("low", "DOUBLE"),
    ("close", "DOUBLE"),
    ("adj_close", "DOUBLE"),
    ("volume", "BIGINT"),
)
_FILE_COLS = ", ".join(name for name, _ in _FILE_TYPES)
_TYPED_FILE_COLS = ", ".join(f"CAST({n} AS {t}) AS {n}" for n, t in _FILE_TYPES)
_GET_COLS = ("ticker", "timestamp", "open", "high", "low", "close", "adj_close", "volume")
_HIVE_TYPES = "{'interval': VARCHAR, 'ticker': VARCHAR, 'year': INTEGER}"
_EMPTY_BARS_SQL = (
    "SELECT CAST(NULL AS VARCHAR) AS ticker, CAST(NULL AS TIMESTAMP) AS timestamp, "
    "CAST(NULL AS VARCHAR) AS interval, CAST(NULL AS DOUBLE) AS open, "
    "CAST(NULL AS DOUBLE) AS high, CAST(NULL AS DOUBLE) AS low, "
    "CAST(NULL AS DOUBLE) AS close, CAST(NULL AS DOUBLE) AS adj_close, "
    "CAST(NULL AS BIGINT) AS volume WHERE false"
)
_CHECKSUM_SQL = (
    "SELECT ticker, interval, COUNT(*) AS n, "
    "SUM(hash(timestamp, open, high, low, close, adj_close, volume)) AS checksum "
    "FROM ({relation}) GROUP BY ticker, interval"
)

PART_FILE = "part-0.parquet"
_SENTINEL = ("_", "_", 0)
_LOCK_DIR = "_locks"


class BarStore(ABC):
    """Storage for the lake's ``bars`` (key ``(ticker, timestamp,
    interval)``). ``DuckDBLake`` routes every bar read and write through
    one of these; SQL elsewhere keeps reading the ``bars`` relation."""

    backend: ClassVar[BarBackend]

    @abstractmethod
    def upsert(self, frame: pd.DataFrame) -> int:
        """Last-write-wins upsert of ``frame`` (columns :data:`BAR_COLUMNS`);
        returns ``len(frame)``."""

    @abstractmethod
    def get(self, ticker: str, interval: Interval, start: Any, end: Any) -> pd.DataFrame:
        """Bars of one series inside ``[start, end]``, oldest first, columns
        ``ticker, timestamp, open, high, low, close, adj_close, volume``."""

    @abstractmethod
    def aggregate(self, ticker: str, source: Interval, target: Interval) -> int:
        """Rebuild ``target`` bars of ``ticker`` from its ``source`` bars;
        returns the net number of new ``target`` rows."""

    @abstractmethod
    def series_checksums(self) -> dict[tuple[str, str], tuple[int, int]]:
        """``{(ticker, interval): (rows, checksum)}`` over every stored bar;
        equal for two stores holding the same bars."""


def checksums(
    con: duckdb.DuckDBPyConnection, relation: str
) -> dict[tuple[str, str], tuple[int, int]]:
    rows = con.execute(_CHECKSUM_SQL.format(relation=relation)).fetchall()
    return {(t, i): (int(n), int(c)) for t, i, n, c in rows}


def _aggregate_sql(source_relation: str, ticker: str, target: Interval) -> str:
    """OHLCV time-bucket aggregation of one ticker's source bars:
    open = first, close = last, high = max, low = min, volume = sum;
    ``adj_close`` tracks ``close`` (no per-bucket corporate actions)."""
    return f"""
        SELECT {_lit(ticker)} AS ticker, bucket AS timestamp, {_lit(target.code)} AS interval,
               open, high, low, close, adj_close, volume
          FROM (
            SELECT time_bucket({target.duckdb_interval}, timestamp) AS bucket,
                   arg_min(open, timestamp) AS open,
                   max(high) AS high,
                   min(low) AS low,
                   arg_max(close, timestamp) AS close,
                   arg_max(close, timestamp) AS adj_close,
                   sum(volume) AS volume
              FROM {source_relation}
             GROUP BY bucket
          )
    """


class DuckDBTableBarStore(BarStore):
    """Bars in the lake's own ``bars`` table."""

    backend: ClassVar[BarBackend] = "duckdb"

    def __init__(self, lake: DuckDBLake) -> None:
        self._lake = lake

    def upsert(self, frame: pd.DataFrame) -> int:
        return self._lake._upsert(frame, table="bars", cols=BAR_COLUMNS, pk=BAR_KEY)

    def get(self, ticker: str, interval: Interval, start: Any, end: Any) -> pd.DataFrame:
        return self._lake.con.execute(
            """
            SELECT ticker, timestamp, open, high, low, close, adj_close, volume
              FROM bars
             WHERE ticker = ? AND interval = ? AND timestamp BETWEEN ? AND ?
             ORDER BY timestamp
            """,
            [ticker, interval.code, start, end],
        ).fetchdf()

    def aggregate(self, ticker: str, source: Interval, target: Interval) -> int:
        con = self._lake.con
        src = (
            f"(SELECT * FROM bars WHERE ticker = {_lit(ticker)} AND interval = {_lit(source.code)})"
        )
        sql = f"""
            INSERT INTO bars ({", ".join(BAR_COLUMNS)})
            {_aggregate_sql(src, ticker, target)}
            ON CONFLICT (ticker, timestamp, interval) DO UPDATE SET
                open = EXCLUDED.open,
                high = EXCLUDED.high,
                low = EXCLUDED.low,
                close = EXCLUDED.close,
                adj_close = EXCLUDED.adj_close,
                volume = EXCLUDED.volume
        """
        # Net new rows over just this ticker's target slice (the INSERT's
        # own row count would include ON CONFLICT updates).
        count_sql = "SELECT COUNT(*) FROM bars WHERE ticker = ? AND interval = ?"
        with self._lake.transaction():
            before = int(con.execute(count_sql, [ticker, target.code]).fetchone()[0])
            con.execute(sql)
            after = int(con.execute(count_sql, [ticker, target.code]).fetchone()[0])
        return after - before

    def series_checksums(self) -> dict[tuple[str, str], tuple[int, int]]:
        return checksums(self._lake.con, "SELECT * FROM bars")


class ParquetBarStore(BarStore):
    """Bars in hive-partitioned Parquet files under ``root`` (see the
    module doc). ``con`` is the DuckDB connection used to read and write
    the files (the lake's own, or any connection); it is switched to
    ``TimeZone = 'UTC'``."""

    backend: ClassVar[BarBackend] = "parquet"

    def __init__(
        self,
        root: str | Path,
        con: duckdb.DuckDBPyConnection,
        *,
        read_only: bool = False,
        lock_timeout: float = 60.0,
    ) -> None:
        self.root = Path(root)
        self.read_only = read_only
        self.lock_timeout = lock_timeout
        self._con = con
        con.execute("SET TimeZone = 'UTC'")

    # ---- layout ------------------------------------------------------------

    @property
    def glob(self) -> str:
        return f"{self.root.as_posix()}/interval=*/ticker=*/year=*/*.parquet"

    def ensure_layout(self) -> None:
        """Create ``root`` and the empty sentinel partition (idempotent)."""
        self._check_writable()
        sentinel = self.partition_dir(*_SENTINEL) / PART_FILE
        if sentinel.exists():
            return
        sentinel.parent.mkdir(parents=True, exist_ok=True)
        empty = f"SELECT {_TYPED_FILE_COLS} FROM ({_EMPTY_BARS_SQL})"
        self._write_file(empty, sentinel)

    def series_dir(self, interval: str, ticker: str) -> Path:
        return self.root / f"interval={_encode(interval)}" / f"ticker={_encode(ticker)}"

    def partition_dir(self, interval: str, ticker: str, year: int) -> Path:
        return self.series_dir(interval, ticker) / f"year={int(year)}"

    def files(
        self, *, tickers: Iterable[str] | None = None, intervals: Iterable[str] | None = None
    ) -> list[Path]:
        """Every partition file (sentinel excluded), optionally narrowed to
        some tickers / interval codes, sorted by path."""
        wanted_t = None if tickers is None else set(tickers)
        wanted_i = None if intervals is None else set(intervals)
        out = []
        for path in self.root.glob("interval=*/ticker=*/year=*/*.parquet"):
            interval, ticker, year = _partition_of(path)
            if (interval, ticker, year) == _SENTINEL:
                continue
            if wanted_t is not None and ticker not in wanted_t:
                continue
            if wanted_i is not None and interval not in wanted_i:
                continue
            out.append(path)
        return sorted(out)

    def view_sql(self) -> str:
        """A SELECT over every stored bar with the ``bars`` table's columns;
        the lake exposes it as the ``bars`` view."""
        if not any(self.root.glob("interval=*/ticker=*/year=*/*.parquet")):
            return _EMPTY_BARS_SQL
        return (
            f"SELECT ticker, timestamp, interval, open, high, low, close, adj_close, volume "
            f"FROM read_parquet({_lit(self.glob)}, hive_partitioning = true, "
            f"hive_types = {_HIVE_TYPES})"
        )

    # ---- reads ---------------------------------------------------------------

    def get(self, ticker: str, interval: Interval, start: Any, end: Any) -> pd.DataFrame:
        series = self.series_dir(interval.code, ticker)
        if not any(series.glob("year=*/*.parquet")):
            return self._con.execute(
                f"SELECT {', '.join(_GET_COLS)} FROM ({_EMPTY_BARS_SQL})"
            ).fetchdf()
        where = "timestamp BETWEEN ? AND ?"
        lo, hi = _year_of(start), _year_of(end)
        if lo is not None and hi is not None:
            where += f" AND year BETWEEN {lo} AND {hi}"
        return self._con.execute(
            f"""
            SELECT CAST(? AS VARCHAR) AS ticker, {_FILE_COLS}
              FROM read_parquet({_lit(series.as_posix() + "/year=*/*.parquet")},
                                hive_partitioning = true, hive_types = {_HIVE_TYPES})
             WHERE {where}
             ORDER BY timestamp
            """,
            [ticker, start, end],
        ).fetchdf()

    def count(self, ticker: str, interval: str) -> int:
        files = sorted(self.series_dir(interval, ticker).glob("year=*/*.parquet"))
        if not files:
            return 0
        return int(
            self._con.execute(f"SELECT COUNT(*) FROM read_parquet({_list(files)})").fetchone()[0]
        )

    def series_checksums(self) -> dict[tuple[str, str], tuple[int, int]]:
        return checksums(self._con, self.view_sql())

    # ---- writes ----------------------------------------------------------------

    def upsert(self, frame: pd.DataFrame) -> int:
        if frame.empty:
            return 0
        self._check_writable()
        # A real copy: DuckDB cannot scan columns with negative strides
        # (e.g. a frame reversed with ``iloc[::-1]``).
        staged = frame[list(BAR_COLUMNS)].reset_index(drop=True).copy(deep=True)
        staged["_ord"] = np.arange(len(staged), dtype=np.int64)
        view = f"_bars_in_{uuid.uuid4().hex}"
        typed = ", ".join(
            [
                "CAST(ticker AS VARCHAR) AS ticker",
                "CAST(interval AS VARCHAR) AS interval",
                _TYPED_FILE_COLS,
                "_ord",
            ]
        )
        self._con.register(view, staged)
        try:
            with self._stage(f"SELECT {typed} FROM {view}") as stage:
                self._write_stage(stage)
        finally:
            self._con.unregister(view)
        return len(frame)

    def aggregate(self, ticker: str, source: Interval, target: Interval) -> int:
        self._check_writable()
        files = sorted(self.series_dir(source.code, ticker).glob("year=*/*.parquet"))
        if not files:
            return 0
        before = self.count(ticker, target.code)
        self.upsert_query(_aggregate_sql(f"read_parquet({_list(files)})", ticker, target))
        return self.count(ticker, target.code) - before

    def upsert_query(self, sql: str) -> None:
        """Upsert the rows of ``sql`` (a SELECT with the bar columns, key
        unique within it) — e.g. a copy from another bar relation."""
        self._check_writable()
        typed = (
            f"SELECT CAST(ticker AS VARCHAR) AS ticker, CAST(interval AS VARCHAR) AS interval, "
            f"{_TYPED_FILE_COLS}, 0 AS _ord FROM ({sql})"
        )
        with self._stage(typed) as stage:
            self._write_stage(stage)

    def export_partitions(
        self,
        target_root: str | Path,
        *,
        tickers: Iterable[str] | None = None,
        end: date | None = None,
    ) -> int:
        """Copy partitions (of ``tickers``, all when None) into a new store
        at ``target_root``, keeping only bars before the day after ``end``.

        Whole partitions are hard-linked when the file system allows it
        (copied otherwise); writers never modify a file in place, so the
        copy does not change when the source is written later. Each series
        is exported under its writer lock, so a concurrent multi-year
        upsert is seen entirely or not at all. Returns the files written."""
        target = ParquetBarStore(target_root, self._con, lock_timeout=self.lock_timeout)
        target.ensure_layout()
        cutoff = None if end is None else datetime(end.year, end.month, end.day) + timedelta(days=1)
        by_series: dict[tuple[str, str], list[Path]] = {}
        for path in self.files(tickers=tickers):
            interval, ticker, _ = _partition_of(path)
            by_series.setdefault((interval, ticker), []).append(path)
        written = 0
        for (interval, ticker), paths in sorted(by_series.items()):
            with self._series_lock(interval, ticker, required=False):
                for path in paths:
                    year = _partition_of(path)[2]
                    if end is not None and year > end.year:
                        continue
                    dst = target.root / path.relative_to(self.root)
                    dst.parent.mkdir(parents=True, exist_ok=True)
                    if cutoff is not None and year == end.year:
                        query = (
                            f"SELECT {_FILE_COLS} FROM read_parquet({_lit(path.as_posix())}) "
                            f"WHERE timestamp < {_lit(cutoff.isoformat(sep=' '))}::TIMESTAMP "
                            f"ORDER BY timestamp"
                        )
                        target._write_file(query, dst)
                    else:
                        _link_or_copy(path, dst)
                    written += 1
        return written

    # ---- internals ---------------------------------------------------------------

    def _check_writable(self) -> None:
        if self.read_only:
            raise duckdb.InvalidInputException(
                f"bar store {self.root} is read-only; writes are refused"
            )

    @contextmanager
    def _stage(self, select_sql: str) -> Iterator[str]:
        name = f"_bars_stage_{uuid.uuid4().hex}"
        self._con.execute(f"CREATE TEMP TABLE {name} AS {select_sql}")
        try:
            nulls = self._con.execute(
                f"SELECT COUNT(*) FROM {name} "
                "WHERE ticker IS NULL OR interval IS NULL OR timestamp IS NULL"
            ).fetchone()[0]
            if nulls:
                raise duckdb.ConstraintException(
                    "NOT NULL constraint failed: bars.ticker, bars.timestamp and "
                    "bars.interval must be set"
                )
            yield name
        finally:
            self._con.execute(f"DROP TABLE IF EXISTS {name}")

    def _write_stage(self, stage: str) -> None:
        groups = self._con.execute(
            f"SELECT DISTINCT interval, ticker, year(timestamp) FROM {stage} ORDER BY ALL"
        ).fetchall()
        by_series: dict[tuple[str, str], list[int]] = {}
        for interval, ticker, year in groups:
            by_series.setdefault((interval, ticker), []).append(int(year))
        self.ensure_layout()
        for (interval, ticker), years in by_series.items():
            self._check_case(interval, ticker)
            with self._series_lock(interval, ticker, required=True):
                for year in years:
                    self._rewrite_partition(stage, interval, ticker, year)

    def _rewrite_partition(self, stage: str, interval: str, ticker: str, year: int) -> None:
        part = self.partition_dir(interval, ticker, year)
        part.mkdir(parents=True, exist_ok=True)
        for stale in part.glob("*.tmp"):
            _unlink_quietly(stale)
        existing = sorted(part.glob("*.parquet"))
        new_rows = (
            f"SELECT {_FILE_COLS}, 1 AS _src, _ord FROM {stage} "
            f"WHERE interval = {_lit(interval)} AND ticker = {_lit(ticker)} "
            f"AND year(timestamp) = {int(year)}"
        )
        source = new_rows
        if existing:
            old_rows = (
                f"SELECT {_TYPED_FILE_COLS}, 0 AS _src, 0 AS _ord "
                f"FROM read_parquet({_list(existing)}, union_by_name = true)"
            )
            source = f"{old_rows} UNION ALL {new_rows}"
        merged = (
            f"SELECT {_FILE_COLS} FROM ({source}) "
            "QUALIFY row_number() OVER (PARTITION BY timestamp ORDER BY _src DESC, _ord DESC) = 1 "
            "ORDER BY timestamp"
        )
        target = part / PART_FILE
        self._write_file(merged, target)
        for path in existing:
            if path.name != PART_FILE:
                self._retry_io(path.unlink)

    def _write_file(self, query: str, target: Path) -> None:
        """Write ``query`` to a temp file beside ``target``, then atomically
        move it over ``target``."""
        tmp = target.with_name(f"{target.name}.{uuid.uuid4().hex}.tmp")
        try:
            self._con.execute(
                f"COPY ({query}) TO {_lit(tmp.as_posix())} (FORMAT parquet, COMPRESSION zstd)"
            )
            self._retry_io(lambda: os.replace(tmp, target))
        except BaseException:
            _unlink_quietly(tmp)
            raise

    def _retry_io(self, op: Any) -> None:
        """Run a rename / delete, retrying while Windows reports the file
        open elsewhere (a reader mid-query), up to ``lock_timeout``."""
        deadline = time.monotonic() + self.lock_timeout
        delay = 0.005
        while True:
            try:
                op()
                return
            except PermissionError:
                if time.monotonic() >= deadline:
                    raise
                time.sleep(delay)
                delay = min(delay * 2, 0.1)

    def _check_case(self, interval: str, ticker: str) -> None:
        series = self.series_dir(interval, ticker)
        parent = series.parent
        if not parent.is_dir():
            return
        names = os.listdir(parent)
        if series.name in names:
            return
        folded = series.name.casefold()
        clash = next((n for n in names if n.casefold() == folded), None)
        if clash is not None:
            raise ValueError(
                f"ticker {ticker!r} differs only by case from the stored series {clash!r}; "
                "the Parquet bar store cannot keep both on case-insensitive file systems"
            )

    @contextmanager
    def _series_lock(self, interval: str, ticker: str, *, required: bool) -> Iterator[None]:
        path = (
            self.root
            / _LOCK_DIR
            / f"interval={_encode(interval)}"
            / f"ticker={_encode(ticker)}.lock"
        )
        lock = FileLock(path, timeout=self.lock_timeout)
        try:
            lock.acquire()
        except TimeoutError:
            raise
        except OSError:
            # A reader exporting from a store it cannot write to.
            if required:
                raise
            yield
            return
        try:
            yield
        finally:
            lock.release()


# ---- helpers -------------------------------------------------------------------


def _lit(value: str) -> str:
    return "'" + str(value).replace("'", "''") + "'"


def _list(paths: Iterable[Path]) -> str:
    return "[" + ", ".join(_lit(p.as_posix()) for p in paths) + "]"


def _encode(value: str) -> str:
    """Percent-encode a partition value for a path segment. A trailing dot
    or space is encoded too: Windows strips them from file names."""
    encoded = quote(str(value), safe="-._~")
    if encoded.endswith("."):
        encoded = encoded[:-1] + "%2E"
    return encoded


def _partition_of(path: Path) -> tuple[str, str, int]:
    year_dir, ticker_dir, interval_dir = path.parent, path.parent.parent, path.parent.parent.parent
    return (
        unquote(interval_dir.name.split("=", 1)[1]),
        unquote(ticker_dir.name.split("=", 1)[1]),
        int(year_dir.name.split("=", 1)[1]),
    )


def _year_of(value: Any) -> int | None:
    """Calendar year (UTC) of a window bound, or None when unknown."""
    if isinstance(value, datetime):
        if value.tzinfo is not None:
            try:
                value = value.astimezone(UTC)
            except (OverflowError, ValueError):
                return None
        return value.year
    if isinstance(value, date):
        return value.year
    try:
        ts = pd.Timestamp(value)
    except (TypeError, ValueError):
        return None
    if pd.isna(ts):
        return None
    if ts.tzinfo is not None:
        ts = ts.tz_convert("UTC")
    return int(ts.year)


def _link_or_copy(src: Path, dst: Path) -> None:
    if dst.exists():
        dst.unlink()
    try:
        os.link(src, dst)
    except OSError:
        shutil.copyfile(src, dst)


def _unlink_quietly(path: Path) -> None:
    with contextlib.suppress(OSError):
        path.unlink()

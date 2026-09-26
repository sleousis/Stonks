"""Parquet bar store under concurrent processes (roadmap 10.4).

One process holds the lake read-write and keeps rewriting bars while four
other processes read the Parquet files directly (they cannot open the
DuckDB file: the writer holds its lock). Readers must never block, never
fail, and never see a half-written partition: every read of a series
returns all of its bars from one single write.
"""

from __future__ import annotations

import multiprocessing
import time
from datetime import date, datetime, timedelta
from pathlib import Path

import duckdb
import pandas as pd

from stonks.core.interval import Interval
from stonks.store.bars import ParquetBarStore
from stonks.store.lake import DuckDBLake

TICKERS = ("A.US", "B.US")
DAYS = 250  # one partition (2024) per ticker


def _version_frame(version: int) -> pd.DataFrame:
    days = [date(2024, 1, 1) + timedelta(days=i) for i in range(DAYS)]
    return pd.DataFrame(
        [
            {
                "ticker": t,
                "date": d,
                "open": float(version),
                "high": float(version),
                "low": float(version),
                "close": float(version),
                "adj_close": float(version),
                "volume": version,
            }
            for t in TICKERS
            for d in days
        ]
    )


def _writer(path: str, versions: int, started, done) -> None:
    with DuckDBLake(Path(path)) as lake:
        started.set()
        for v in range(2, versions + 1):
            lake.upsert_prices(_version_frame(v))
    done.set()


def _reader(root: str, done, out) -> None:
    con = duckdb.connect()
    store = ParquetBarStore(Path(root), con, read_only=True)
    reads, errors, seen = 0, [], set()
    last = dict.fromkeys(TICKERS, 0)
    try:
        while True:
            finished = done.is_set()
            for ticker in TICKERS:
                got = store.get(ticker, Interval.DAY_1, datetime(2024, 1, 1), datetime(2025, 1, 1))
                closes = set(got["close"])
                if len(got) != DAYS or len(closes) != 1:
                    errors.append(f"{ticker}: {len(got)} rows, closes {sorted(closes)[:3]}")
                    continue
                version = int(closes.pop())
                if version < last[ticker]:
                    errors.append(f"{ticker}: went back from {last[ticker]} to {version}")
                last[ticker] = version
                seen.add(version)
            whole = con.execute(
                f"SELECT ticker, COUNT(*), COUNT(DISTINCT close) FROM ({store.view_sql()}) "
                "GROUP BY ticker"
            ).fetchall()
            for ticker, n, distinct in whole:
                if n != DAYS or distinct != 1:
                    errors.append(f"view {ticker}: {n} rows, {distinct} versions")
            reads += 1
            if finished:
                break
    except Exception as exc:  # pragma: no cover - reported to the parent
        errors.append(repr(exc))
    out.put((reads, errors, sorted(seen)))


def test_readers_never_block_or_see_partial_writes_while_a_writer_upserts(tmp_path):
    path = tmp_path / "lake.duckdb"
    with DuckDBLake(path, bar_backend="parquet") as lake:
        lake.migrate()
        lake.upsert_prices(_version_frame(1))
    ctx = multiprocessing.get_context("spawn")
    started, done, out = ctx.Event(), ctx.Event(), ctx.Queue()
    readers = [
        ctx.Process(target=_reader, args=(str(tmp_path / "bars"), done, out)) for _ in range(4)
    ]
    for p in readers:
        p.start()
    writer = ctx.Process(target=_writer, args=(str(path), 40, started, done))
    writer.start()
    results = [out.get(timeout=240) for _ in readers]
    writer.join(timeout=60)
    for p in readers:
        p.join(timeout=60)
    assert writer.exitcode == 0
    for reads, errors, _seen in results:
        assert errors == []
        assert reads > 0
    # the readers really ran alongside the writer
    assert max(len(seen) for _, _, seen in results) > 1
    with DuckDBLake(path, read_only=True) as lake:
        final = lake.get_prices("A.US", date(2024, 1, 1), date(2025, 1, 1))
        assert set(final["close"]) == {40.0}
    assert not list((tmp_path / "bars").rglob("*.tmp"))


def _racer(root: str, value: float, rounds: int, go) -> None:
    con = duckdb.connect()
    store = ParquetBarStore(Path(root), con)
    frame = _version_frame(int(value)).rename(columns={"date": "timestamp"})
    frame["timestamp"] = pd.to_datetime(frame["timestamp"])
    frame["interval"] = "1d"
    go.wait(30)
    for _ in range(rounds):
        store.upsert(frame)
        time.sleep(0)


def test_two_writers_on_one_series_never_corrupt_it(tmp_path):
    root = tmp_path / "bars"
    ctx = multiprocessing.get_context("spawn")
    go = ctx.Event()
    writers = [ctx.Process(target=_racer, args=(str(root), v, 15, go)) for v in (7.0, 9.0)]
    for p in writers:
        p.start()
    go.set()
    for p in writers:
        p.join(timeout=240)
        assert p.exitcode == 0
    con = duckdb.connect()
    store = ParquetBarStore(root, con, read_only=True)
    for ticker in TICKERS:
        got = store.get(ticker, Interval.DAY_1, datetime(2024, 1, 1), datetime(2025, 1, 1))
        assert len(got) == DAYS
        assert len(set(got["close"])) == 1 and got["close"].iloc[0] in (7.0, 9.0)
    parts = [p for p in root.rglob("*") if p.is_file() and "_locks" not in p.parts]
    assert all(p.name == "part-0.parquet" for p in parts)

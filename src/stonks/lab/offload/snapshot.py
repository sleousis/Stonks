"""Read-only lake snapshots for lab workers (roadmap 14.9).

DuckDB lets one process hold a lake file read-write, and while it does no
other process may open it at all. So the process that holds the lake (the
API) publishes a copy, and lab workers open the copy ``read_only``:

    <root>/CURRENT.json            which snapshot is current
    <root>/<stamp>/lake.duckdb     every table and view of the lake
    <root>/<stamp>/bars/...        Parquet bars, hard-linked (Parquet lakes)
    <root>/<stamp>/holds/<worker>  a worker is reading this snapshot

With the Parquet bar store the copy holds only the small tables, and the
bar partitions are hard links (writers replace files, never modify them),
so publishing is cheap. With the DuckDB bar table the bars are copied too.

A snapshot is rebuilt when the lake changed since (a newer ingest run,
bar fetch, or universe change) or it is older than a maximum age. Old snapshots are pruned,
except ones a worker holds.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import threading
import time
from collections.abc import Callable, Iterator
from contextlib import AbstractContextManager, contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

from stonks.logging import get_logger
from stonks.store.lake import DuckDBLake

_log = get_logger("stonks.lab.offload.snapshot")

CURRENT_FILE = "CURRENT.json"
LAKE_FILE = "lake.duckdb"
_HOLDS = "holds"
#: A snapshot folder name (a UTC stamp, maybe with a ``-N`` suffix).
SNAPSHOT_NAME = re.compile(r"[0-9A-Za-z][0-9A-Za-z_-]{0,63}")


@dataclass(frozen=True)
class SnapshotInfo:
    #: The snapshot's lake file (open it with ``DuckDBLake(path, read_only=True)``).
    path: Path
    created_at: datetime
    #: :func:`lake_fingerprint` of the lake when the snapshot was taken.
    fingerprint: str

    @property
    def directory(self) -> Path:
        return self.path.parent

    def open(self) -> DuckDBLake:
        return DuckDBLake(self.path, read_only=True)


#: Small tables a lab run reads that change outside an ingest run: stored
#: universes, their membership and index histories (BE-19). Each is hashed
#: whole, row by row, so any insert, update or delete shows.
_HASHED_TABLES = (
    "universe_definitions",
    "universe_membership",
    "index_constituent_snapshots",
    "index_constituent_changes",
)


def lake_fingerprint(lake: DuckDBLake) -> str:
    """Changes whenever an ingest run finishes, bars are fetched on
    demand, or a universe, its membership or an index history changes:
    the ways data lands in a running lake."""
    parts: list[str] = []
    for table, column in (("ingest_runs", "finished_at"), ("bar_fetch_ranges", "fetched_at")):
        parts.append(_part(lake, f"SELECT MAX({column}), COUNT(*) FROM {table}"))
    for table in _HASHED_TABLES:
        parts.append(_part(lake, f"SELECT bit_xor(hash(t)), COUNT(*) FROM {table} t"))
    return "|".join(parts)


def _part(lake: DuckDBLake, sql: str) -> str:
    try:
        row = lake.con.execute(sql).fetchone()
    except Exception:  # an older lake without the table
        row = None
    return "-" if row is None else f"{row[0]}#{row[1]}"


class LakeSnapshots:
    def __init__(
        self,
        root: str | Path,
        *,
        keep: int = 2,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self.root = Path(root)
        self._keep = max(1, keep)
        self._clock = clock or (lambda: datetime.now(UTC))
        self._lock = threading.Lock()

    # ---- reads ---------------------------------------------------------------

    def current(self) -> SnapshotInfo | None:
        marker = self.root / CURRENT_FILE
        try:
            meta = json.loads(marker.read_text(encoding="utf-8"))
        except (FileNotFoundError, ValueError):
            return None
        path = self.root / meta["dir"] / LAKE_FILE
        if not path.exists():
            return None
        return SnapshotInfo(
            path=path,
            created_at=datetime.fromisoformat(meta["created_at"]),
            fingerprint=str(meta.get("fingerprint", "")),
        )

    def get(self, name: str) -> SnapshotInfo | None:
        """The snapshot in folder ``name`` (current or not), or ``None``."""
        if not SNAPSHOT_NAME.fullmatch(name):
            return None
        current = self.current()
        if current is not None and current.directory.name == name:
            return current
        path = self.root / name / LAKE_FILE
        if not path.is_file():
            return None
        return SnapshotInfo(
            path=path,
            created_at=datetime.fromtimestamp(path.stat().st_mtime, UTC),
            fingerprint="",
        )

    def adopt(self, name: str, created_at: datetime, fingerprint: str) -> SnapshotInfo:
        """Make the already-filled folder ``name`` current (a remote lab
        worker's downloaded copy of the server's snapshot)."""
        if not SNAPSHOT_NAME.fullmatch(name) or not (self.root / name / LAKE_FILE).is_file():
            raise FileNotFoundError(f"no snapshot folder {name!r} under {self.root}")
        with self._lock:
            meta = {"dir": name, "created_at": created_at.isoformat(), "fingerprint": fingerprint}
            tmp = self.root / f".{CURRENT_FILE}.{os.getpid()}.tmp"
            tmp.write_text(json.dumps(meta), encoding="utf-8")
            os.replace(tmp, self.root / CURRENT_FILE)
        self.prune()
        info = self.current()
        assert info is not None
        return info

    def files(self, info: SnapshotInfo) -> list[Path]:
        """Every file of ``info``'s folder except the hold marks."""
        root = info.directory
        return sorted(
            p for p in root.rglob("*") if p.is_file() and p.relative_to(root).parts[0] != _HOLDS
        )

    def is_stale(self, info: SnapshotInfo | None, lake: DuckDBLake, max_age_minutes: float) -> bool:
        if info is None:
            return True
        if self._clock() - info.created_at > timedelta(minutes=max_age_minutes):
            return True
        return lake_fingerprint(lake) != info.fingerprint

    # ---- writes --------------------------------------------------------------

    def publish(self, lake: DuckDBLake) -> SnapshotInfo:
        """Copy ``lake`` into a new snapshot and make it current."""
        with self._lock:
            return self._publish(lake)

    def ensure_fresh(
        self,
        open_lake: Callable[[], AbstractContextManager[DuckDBLake]],
        max_age_minutes: float,
    ) -> SnapshotInfo:
        """The current snapshot, rebuilt first when it is stale."""
        with self._lock:
            info = self.current()
            with open_lake() as lake:
                if not self.is_stale(info, lake, max_age_minutes):
                    assert info is not None
                    return info
                return self._publish(lake)

    def _publish(self, lake: DuckDBLake) -> SnapshotInfo:
        started = time.perf_counter()
        now = self._clock()
        fingerprint = lake_fingerprint(lake)
        name = now.strftime("%Y%m%dT%H%M%S%fZ")
        directory = self.root / name
        suffix = 0
        while directory.exists():
            suffix += 1
            directory = self.root / f"{name}-{suffix}"
        lake.export_database(directory / LAKE_FILE)
        meta = {"dir": directory.name, "created_at": now.isoformat(), "fingerprint": fingerprint}
        tmp = self.root / f".{CURRENT_FILE}.{os.getpid()}.tmp"
        tmp.write_text(json.dumps(meta), encoding="utf-8")
        os.replace(tmp, self.root / CURRENT_FILE)
        info = SnapshotInfo(path=directory / LAKE_FILE, created_at=now, fingerprint=fingerprint)
        _log.info(
            "lab_snapshot.published",
            snapshot=directory.name,
            seconds=round(time.perf_counter() - started, 3),
        )
        self.prune()
        return info

    @contextmanager
    def hold(self, info: SnapshotInfo, holder: str) -> Iterator[Callable[[], None]]:
        """Mark ``info`` in use by ``holder`` so :meth:`prune` keeps it.
        Yields a function that refreshes the mark (call it on heartbeats)."""
        holds = info.directory / _HOLDS
        holds.mkdir(parents=True, exist_ok=True)
        mark = holds / holder
        mark.touch()
        try:
            yield mark.touch
        finally:
            mark.unlink(missing_ok=True)

    def prune(self, hold_ttl_seconds: float = 3600.0) -> list[Path]:
        """Delete snapshots beyond the newest ``keep``, skipping the current
        one and any held within ``hold_ttl_seconds``. A snapshot whose
        files are still open (Windows) is left for the next prune."""
        current = self.current()
        dirs = sorted(
            (p for p in self.root.iterdir() if p.is_dir() and (p / LAKE_FILE).exists()),
            key=lambda p: p.name,
            reverse=True,
        )
        removed: list[Path] = []
        for directory in dirs[self._keep :]:
            if current is not None and directory == current.directory:
                continue
            if self._held(directory, hold_ttl_seconds):
                continue
            shutil.rmtree(directory, ignore_errors=True)
            if not directory.exists():
                removed.append(directory)
        return removed

    def _held(self, directory: Path, ttl: float) -> bool:
        holds = directory / _HOLDS
        if not holds.is_dir():
            return False
        cutoff = time.time() - ttl
        return any(mark.stat().st_mtime >= cutoff for mark in holds.iterdir())

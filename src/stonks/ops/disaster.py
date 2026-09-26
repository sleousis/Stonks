"""Disaster restore from an off-server snapshot (TO-01).

``deploy/backup/restore.sh`` restores a restic snapshot into a scratch
folder and then calls :func:`restore_snapshot` to bring it into the data
folder. ``deploy/backup/restore-test.sh`` does the same into a throwaway
folder and calls :func:`check_restored`. Keeping the logic here means it is
tested without Docker or restic.

A restic snapshot holds one of two layouts:

- **app backups**: ``.../backups/stonks-<id>/`` folders made by
  :mod:`stonks.ops.backup`. The newest one is restored with
  :func:`stonks.ops.restore.restore_backup`.
- **a stopped-volume copy**: ``lake.duckdb``, ``state.sqlite``,
  ``artifacts/`` and ``bars/`` straight in one folder.

Either way the state DB, the artifacts and the lake come back together. A
snapshot that lacks the state DB or the lake (or the artifacts while the
state lists strategies) is refused before anything is touched. Data already
in the target is moved aside to ``<name>.pre-restore-<stamp>``, never
deleted.
"""

from __future__ import annotations

import contextlib
import sqlite3
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal

from stonks.logging import get_logger
from stonks.ops.backup import _ID_RE, MANIFEST, DataPaths, read_manifest
from stonks.ops.restore import (
    _aside_name,
    _place_file,
    _place_tree,
    _roll_back,
    occupied_paths,
    restore_backup,
)
from stonks.store.lake import DuckDBLake
from stonks.store.state import SqliteState

#: State tables whose row counts prove a restore kept the accounts, the
#: strategies and the ledger.
CHECKED_TABLES: tuple[str, ...] = ("users", "strategies", "orders")

_log = get_logger("stonks.ops.disaster")


class DisasterRestoreError(RuntimeError):
    """The snapshot cannot be restored. Nothing in the target was changed."""


@dataclass(frozen=True)
class Snapshot:
    kind: Literal["app_backup", "volume"]
    path: Path


@dataclass
class DisasterRestoreResult:
    snapshot: Snapshot
    expected: dict[str, int]
    moved_aside: list[Path] = field(default_factory=list)


def find_snapshot(root: str | Path) -> Snapshot:
    """The data to restore from a ``restic restore`` tree: the newest app
    backup, else the folder holding a stopped-volume copy."""
    root = Path(root)
    backups = [
        p.parent for p in root.rglob(MANIFEST) if p.parent.is_dir() and _ID_RE.match(p.parent.name)
    ]
    if backups:
        return Snapshot("app_backup", max(backups, key=lambda p: p.name))
    folders = {p.parent for name in ("state.sqlite", "lake.duckdb") for p in root.rglob(name)}
    if not folders:
        raise DisasterRestoreError(f"no Stonks data (app backup, lake or state) under {root}")
    return Snapshot("volume", min(folders, key=lambda p: (len(p.parts), str(p))))


def expected_counts(snapshot: Snapshot) -> dict[str, int]:
    """Row counts of :data:`CHECKED_TABLES` in the snapshot. Raises
    :class:`DisasterRestoreError` when it has no usable state DB."""
    if snapshot.kind == "app_backup":
        state = read_manifest(snapshot.path).get("state")
        if not state:
            raise DisasterRestoreError(f"backup {snapshot.path} has no state DB")
        counts = state.get("row_counts", {})
        missing = [t for t in CHECKED_TABLES if t not in counts]
        if missing:
            raise DisasterRestoreError(
                f"backup {snapshot.path} state DB has no {', '.join(missing)} table"
            )
        return {t: int(counts[t]) for t in CHECKED_TABLES}
    state_path = DataPaths.under(snapshot.path).state
    problem = _state_file_problem(state_path)
    if problem:
        raise DisasterRestoreError(f"snapshot {snapshot.path}: {problem}")
    counts, missing = _read_counts(state_path, CHECKED_TABLES)
    if missing:
        raise DisasterRestoreError(
            f"snapshot {snapshot.path} state DB has no {', '.join(missing)} table"
        )
    return counts


def restore_snapshot(
    root: str | Path, data_dir: str | Path, *, now: datetime | None = None
) -> DisasterRestoreResult:
    """Restore the state DB, the artifacts and the lake from ``root`` into
    ``data_dir``, run migrations, and check the row counts came back."""
    snapshot = find_snapshot(root)
    expected = expected_counts(snapshot)
    _check_complete(snapshot, expected)
    target = DataPaths.under(data_dir)
    if snapshot.kind == "app_backup":
        try:
            restored = restore_backup(snapshot.path, target, force=True, now=now)
        except Exception as exc:
            raise DisasterRestoreError(str(exc)) from exc
        moved = restored.moved_aside
    else:
        moved = _restore_volume(DataPaths.under(snapshot.path), target, now)
    result = DisasterRestoreResult(snapshot=snapshot, expected=expected, moved_aside=moved)
    problems = check_restored(data_dir, expected)
    if problems:
        raise DisasterRestoreError(
            "restored data failed the check (the old data is kept at "
            + ", ".join(str(p) for p in moved)
            + "): "
            + "; ".join(problems)
        )
    _log.info(
        "disaster.restore.done",
        snapshot=str(snapshot.path),
        kind=snapshot.kind,
        expected=expected,
        moved_aside=[str(p) for p in moved],
    )
    return result


def check_restored(data_dir: str | Path, expected: Mapping[str, int]) -> list[str]:
    """Problems with a restored data folder; empty when it is good. The
    state DB must exist, be non-empty and hold at least ``expected`` rows
    per table; the lake must exist; the artifacts must exist when the
    state lists strategies."""
    paths = DataPaths.under(data_dir)
    problems: list[str] = []
    problem = _state_file_problem(paths.state)
    if problem:
        problems.append(problem)
    else:
        counts, missing = _read_counts(paths.state, tuple(expected))
        problems += [f"state table {t} is missing" for t in missing]
        for table, want in expected.items():
            got = counts.get(table)
            if got is not None and got < want:
                problems.append(f"{table}: {got} row(s), the snapshot has {want}")
        if counts.get("strategies", 0) > 0 and not _has_files(paths.artifacts):
            problems.append(f"artifacts folder {paths.artifacts} is missing or empty")
    if not paths.lake.is_file() or paths.lake.stat().st_size == 0:
        problems.append(f"lake {paths.lake} is missing or empty")
    return problems


# ---- internals ---------------------------------------------------------------


def _check_complete(snapshot: Snapshot, expected: Mapping[str, int]) -> None:
    """Refuse a snapshot that would restore only part of the data."""
    if snapshot.kind == "app_backup":
        manifest = read_manifest(snapshot.path)
        has_lake = bool(manifest.get("lake"))
        has_artifacts = bool(manifest.get("artifacts"))
    else:
        source = DataPaths.under(snapshot.path)
        has_lake = source.lake.is_file() and source.lake.stat().st_size > 0
        has_artifacts = source.artifacts.is_dir()
    if not has_lake:
        raise DisasterRestoreError(f"snapshot {snapshot.path} has no lake; nothing was changed")
    if expected.get("strategies", 0) > 0 and not has_artifacts:
        raise DisasterRestoreError(
            f"snapshot {snapshot.path} lists strategies but has no artifacts; nothing was changed"
        )


def _restore_volume(source: DataPaths, target: DataPaths, now: datetime | None) -> list[Path]:
    stamp = (now or datetime.now(UTC)).strftime("%Y%m%dT%H%M%SZ")
    occupied = occupied_paths(target)
    moved: list[tuple[Path, Path]] = []
    try:
        for path in occupied:
            aside = _aside_name(path, target, stamp)
            path.rename(aside)
            moved.append((path, aside))
        for db, dst, suffixes in (
            (source.state, target.state, ("-wal", "-shm")),
            (source.lake, target.lake, (".wal",)),
        ):
            for suffix in suffixes:
                companion = db.with_name(db.name + suffix)
                if companion.is_file():
                    _place_file(companion, dst.with_name(dst.name + suffix))
            _place_file(db, dst)
        if source.bars_root.is_dir():
            _place_tree(source.bars_root, target.bars_root)
        if source.artifacts.is_dir():
            _place_tree(source.artifacts, target.artifacts)
        with DuckDBLake(target.lake) as lake:
            lake.migrate()
        with SqliteState(target.state) as state:
            state.migrate()
    except Exception as exc:
        untouched = set(occupied) - {original for original, _ in moved}
        _roll_back(target, moved, untouched)
        raise DisasterRestoreError(f"restore failed and was rolled back: {exc}") from exc
    return [aside for _, aside in moved]


def _state_file_problem(path: Path) -> str | None:
    if not path.is_file():
        return f"state DB {path.name} is missing ({path})"
    if path.stat().st_size == 0:
        return f"state DB {path.name} is empty ({path})"
    return None


def _read_counts(path: Path, tables: tuple[str, ...]) -> tuple[dict[str, int], list[str]]:
    """Row counts per table, read-only, and the tables that do not exist."""
    con = sqlite3.connect(f"{path.resolve().as_uri()}?mode=ro", uri=True)
    try:
        present = {r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
        counts = {
            t: int(con.execute(f'SELECT COUNT(*) FROM "{t}"').fetchone()[0])
            for t in tables
            if t in present
        }
    except sqlite3.DatabaseError as exc:
        return {}, [f"{t} (unreadable: {exc})" for t in tables]
    finally:
        with contextlib.suppress(sqlite3.Error):
            con.close()
    return counts, [t for t in tables if t not in present]


def _has_files(folder: Path) -> bool:
    return folder.is_dir() and any(p.is_file() for p in folder.rglob("*"))

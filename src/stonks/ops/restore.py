"""Restore a backup made by :mod:`stonks.ops.backup` (roadmap 12.4).

The restore:

1. verifies the backup (checksums, row counts) and refuses a bad one;
2. refuses a backup whose schema is newer than this code's migrations;
3. refuses a target that already holds data (lake file, Parquet bars,
   state DB and its WAL files, a non-empty artifacts dir) unless
   ``force``; with ``force`` that data is moved aside to
   ``<name>.pre-restore-<timestamp>``, never deleted;
4. copies each store in under a temporary name and renames it into place;
5. runs both stores' migrations, so an older backup comes up on the
   current schema.

Stop every Stonks process (server, scheduler, CLI) before restoring: the
restore replaces files underneath them.
"""

from __future__ import annotations

import contextlib
import os
import shutil
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

from stonks.logging import get_logger
from stonks.ops.backup import (
    ARTIFACTS,
    LAKE_BARS,
    LAKE_FILE,
    STATE_FILE,
    DataPaths,
    read_manifest,
    verify_backup,
)
from stonks.store import lake as lake_module
from stonks.store import state as state_module
from stonks.store.lake import DuckDBLake
from stonks.store.state import SqliteState

_log = get_logger("stonks.ops.restore")


class RestoreError(RuntimeError):
    """The restore was refused or failed; the target is left as it was
    unless the message says otherwise."""


@dataclass
class RestoreResult:
    backup: Path
    target: DataPaths
    moved_aside: list[Path] = field(default_factory=list)
    lake_migrations_applied: list[int] = field(default_factory=list)
    state_migrations_applied: list[int] = field(default_factory=list)


def restore_backup(
    backup: str | Path,
    target: DataPaths,
    *,
    force: bool = False,
    now: datetime | None = None,
) -> RestoreResult:
    backup = Path(backup)
    report = verify_backup(backup)
    if not report.ok:
        raise RestoreError("backup failed verification: " + "; ".join(report.problems[:5]))
    manifest = read_manifest(backup)
    _check_schema("lake", manifest.get("lake"), lake_module.MIGRATIONS_DIR)
    _check_schema("state", manifest.get("state"), state_module.MIGRATIONS_DIR)

    occupied = occupied_paths(target)
    if occupied and not force:
        raise RestoreError(
            "the target is not empty: "
            + ", ".join(str(p) for p in occupied)
            + ". Restore into an empty data dir, or pass --force to move this data aside."
        )
    stamp = (now or datetime.now(UTC)).strftime("%Y%m%dT%H%M%SZ")
    result = RestoreResult(backup=backup, target=target)
    moved: list[tuple[Path, Path]] = []
    try:
        for path in occupied:
            aside = _aside_name(path, target, stamp)
            path.rename(aside)
            moved.append((path, aside))
        _place_all(backup, manifest, target)
        with DuckDBLake(target.lake) as lake:
            before = set(lake.applied_migrations()) if manifest.get("lake") else set()
            lake.migrate()
            result.lake_migrations_applied = sorted(set(lake.applied_migrations()) - before)
        with SqliteState(target.state) as state:
            before = set(state.applied_migrations())
            state.migrate()
            result.state_migrations_applied = sorted(set(state.applied_migrations()) - before)
    except Exception as exc:
        untouched = set(occupied) - {original for original, _ in moved}
        _roll_back(target, moved, untouched)
        raise RestoreError(f"restore failed and was rolled back: {exc}") from exc
    result.moved_aside = [aside for _, aside in moved]
    _log.info(
        "restore.done",
        backup=str(backup),
        moved_aside=[str(p) for p in result.moved_aside],
        lake_migrations=result.lake_migrations_applied,
        state_migrations=result.state_migrations_applied,
    )
    return result


def occupied_paths(target: DataPaths) -> list[Path]:
    """Existing data a restore into ``target`` would replace."""
    candidates = [
        target.lake,
        target.lake.with_name(target.lake.name + ".wal"),
        target.bars_root,
        target.state,
        target.state.with_name(target.state.name + "-wal"),
        target.state.with_name(target.state.name + "-shm"),
    ]
    out = [p for p in candidates if p.exists()]
    if (target.artifacts.is_dir() and any(target.artifacts.iterdir())) or (
        target.artifacts.exists() and not target.artifacts.is_dir()
    ):
        out.append(target.artifacts)
    return out


def _aside_name(path: Path, target: DataPaths, stamp: str) -> Path:
    """``<name>.pre-restore-<stamp>``, keeping a database's WAL companions
    paired with it (SQLite looks for ``<db>-wal``, DuckDB for ``<db>.wal``),
    so the set-aside copy still opens with its uncheckpointed writes."""
    for db in (target.state, target.lake):
        if path != db and path.name.startswith(db.name) and path.parent == db.parent:
            suffix = path.name[len(db.name) :]
            return path.with_name(f"{db.name}.pre-restore-{stamp}{suffix}")
    return path.with_name(f"{path.name}.pre-restore-{stamp}")


def _check_schema(name: str, facts: dict | None, migrations_dir: Path) -> None:
    if not facts or not facts.get("schema_versions"):
        return
    known = max(int(p.stem.split("_", 1)[0]) for p in migrations_dir.glob("*.sql"))
    newest = max(facts["schema_versions"])
    if newest > known:
        raise RestoreError(
            f"the backup's {name} schema (migration {newest}) is newer than this code "
            f"(migration {known}); restore with the version of Stonks that made it"
        )


def _place_all(backup: Path, manifest: dict, target: DataPaths) -> None:
    if manifest.get("state"):
        _place_file(backup / STATE_FILE, target.state)
    if manifest.get("lake"):
        _place_file(backup / LAKE_FILE, target.lake)
        if (backup / LAKE_BARS).is_dir():
            _place_tree(backup / LAKE_BARS, target.bars_root)
    if manifest.get("artifacts"):
        _place_tree(backup / ARTIFACTS, target.artifacts)


def _roll_back(target: DataPaths, moved: list[tuple[Path, Path]], untouched: set[Path]) -> None:
    """Undo a failed restore: remove what the restore placed (and temp
    copies), never the ``untouched`` original data it had not yet moved
    aside, then move the set-aside data back."""
    placed = [p for p in occupied_paths(target) if p not in untouched]
    for path in [*placed, *_restoring(target)]:
        with contextlib.suppress(OSError):
            if path.is_dir():
                shutil.rmtree(path)
            else:
                path.unlink()
    for original, aside in reversed(moved):
        with contextlib.suppress(OSError):
            aside.rename(original)


def _restoring(target: DataPaths) -> list[Path]:
    paths = (target.lake, target.bars_root, target.state, target.artifacts)
    return [
        p.with_name(p.name + ".restoring")
        for p in paths
        if p.with_name(p.name + ".restoring").exists()
    ]


def _place_file(src: Path, dst: Path) -> None:
    dst.parent.mkdir(parents=True, exist_ok=True)
    tmp = dst.with_name(dst.name + ".restoring")
    shutil.copyfile(src, tmp)
    os.replace(tmp, dst)


def _place_tree(src: Path, dst: Path) -> None:
    dst.parent.mkdir(parents=True, exist_ok=True)
    tmp = dst.with_name(dst.name + ".restoring")
    if tmp.exists():
        shutil.rmtree(tmp)
    # Plain copies, not hard links: the restored store must not share
    # files with the backup it came from.
    shutil.copytree(src, tmp)
    if dst.exists():
        # Only an empty artifacts dir can be here (anything else was
        # refused or moved aside above).
        dst.rmdir()
    tmp.rename(dst)

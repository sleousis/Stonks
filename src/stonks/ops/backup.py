"""Consistent backups of the lake, the state DB and the artifacts dir
(roadmap 12.4).

A backup is one folder, ``stonks-<UTC timestamp>Z``::

    manifest.json            what is inside: sizes, SHA-256s, schema versions,
                             row counts, git sha, stonks version
    state/state.sqlite       SQLite online-backup API copy (one consistent
                             snapshot even while the tick writes)
    lake/lake.duckdb         ``COPY FROM DATABASE`` of the lake: one statement,
                             so one transaction snapshot even under writes
    lake/bars/...            Parquet bar store only: partitions hard-linked
                             (copied across file systems). Writers never
                             modify a partition in place (they ``os.replace``
                             it) and each series is taken under its writer
                             lock, so every file is whole and every series
                             is from one moment.
    artifacts/...            copy of the registry's artifact bundles

It is built as ``<id>.partial`` and renamed when complete, so a crash never
leaves something that looks like a backup. The three stores are
snapshotted one after the other, not atomically together; they are not
transactionally coupled in normal operation either.

DuckDB lets one process hold a lake for writing. A backup from another
process opens the lake read-only and fails with :class:`BackupError` while
a writer (``stonks serve``) holds it; the in-process path (``lake=``, for
a scheduled job inside the server) copies through the open lake instead.

Where backups end up is a :class:`BackupTarget`: the local file system
today; off-server targets (restic, S3; roadmap 14.5) implement the same
four methods.
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import os
import platform
import re
import shutil
import sqlite3
import subprocess
import tempfile
import time
import uuid
from abc import ABC, abstractmethod
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from importlib import metadata
from pathlib import Path
from typing import Any

import duckdb

from stonks.logging import get_logger
from stonks.ops.config import BackupRetention
from stonks.store.bars import ParquetBarStore
from stonks.store.lake import DuckDBLake

FORMAT_VERSION = 1
MANIFEST = "manifest.json"
PARTIAL_SUFFIX = ".partial"
STATE_FILE = "state/state.sqlite"
LAKE_FILE = "lake/lake.duckdb"
LAKE_BARS = "lake/bars"
ARTIFACTS = "artifacts"
_ID_RE = re.compile(r"^stonks-\d{8}T\d{6}Z(-\d+)?$")


def backup_id_order(backup_id: str) -> tuple[str, int]:
    """Sort key of a backup id: its timestamp, then its same-second counter
    as a number (by name ``-10`` would sort before ``-9``)."""
    stamp, _, counter = backup_id.partition("Z-")
    return (stamp.rstrip("Z"), int(counter) if counter.isdigit() else 0)


_CHUNK = 1 << 20

_log = get_logger("stonks.ops.backup")


class BackupError(RuntimeError):
    """A backup could not be taken, stored or found."""


@dataclass(frozen=True)
class DataPaths:
    """Where one Stonks install keeps its data."""

    lake: Path
    state: Path
    artifacts: Path

    @property
    def bars_root(self) -> Path:
        """The Parquet bar store's folder (``DuckDBLake.bars_root``)."""
        return self.lake.parent / "bars"

    @classmethod
    def under(cls, data_dir: str | Path) -> DataPaths:
        """The default layout inside one folder (``STONKS_DATA_DIR``)."""
        base = Path(data_dir)
        return cls(base / "lake.duckdb", base / "state.sqlite", base / "artifacts")

    @classmethod
    def from_settings(cls, settings: Any) -> DataPaths:
        return cls(
            lake=Path(settings.lake.path),
            state=Path(settings.state.path),
            artifacts=Path(settings.registry.artifacts_dir),
        )


@dataclass(frozen=True)
class BackupRef:
    id: str
    created_at: datetime


@dataclass
class VerifyReport:
    backup: Path
    problems: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.problems


# ---- targets ---------------------------------------------------------------------


class BackupTarget(ABC):
    """Where finished backups are kept. ``list`` returns newest first."""

    @abstractmethod
    def put(self, local: Path) -> BackupRef: ...

    @abstractmethod
    def list(self) -> list[BackupRef]: ...

    @abstractmethod
    def fetch(self, backup_id: str, workdir: Path | None = None) -> Path:
        """A local folder holding the backup (downloaded into ``workdir``
        by remote targets)."""

    @abstractmethod
    def delete(self, backup_id: str) -> None: ...

    def staging_dir(self) -> Path | None:
        """Where :func:`run_backup` should build a backup before ``put``
        (the same file system makes ``put`` a rename and lets Parquet bars
        be hard-linked); ``None`` for a temporary folder."""
        return None


class LocalFilesystemTarget(BackupTarget):
    """Backups as folders under ``root``."""

    def __init__(self, root: str | Path) -> None:
        self.root = Path(root)

    def staging_dir(self) -> Path:
        return self.root

    def put(self, local: Path) -> BackupRef:
        local = Path(local)
        manifest = read_manifest(local)
        dest = self.root / local.name
        if local.resolve() != dest.resolve():
            if dest.exists():
                raise BackupError(f"backup {dest} already exists")
            self.root.mkdir(parents=True, exist_ok=True)
            shutil.move(str(local), str(dest))
        return _ref(manifest)

    def list(self) -> list[BackupRef]:
        if not self.root.is_dir():
            return []
        refs = []
        for path in self.root.iterdir():
            if not path.is_dir() or not _ID_RE.match(path.name):
                continue
            try:
                refs.append(_ref(read_manifest(path)))
            except BackupError:
                continue
        return sorted(refs, key=lambda r: r.created_at, reverse=True)

    def fetch(self, backup_id: str, workdir: Path | None = None) -> Path:
        path = self._path(backup_id)
        if not (path / MANIFEST).is_file():
            raise BackupError(f"no backup {backup_id!r} under {self.root}")
        return path

    def delete(self, backup_id: str) -> None:
        shutil.rmtree(self._path(backup_id))

    def _path(self, backup_id: str) -> Path:
        if not _ID_RE.match(backup_id):
            raise BackupError(f"not a backup id: {backup_id!r}")
        return self.root / backup_id


# ---- create ----------------------------------------------------------------------


def create_backup(
    paths: DataPaths,
    dest_root: str | Path,
    *,
    lake: DuckDBLake | None = None,
    now: datetime | None = None,
) -> Path:
    """Snapshot ``paths`` into a new folder under ``dest_root`` and return
    it. Pass the process's open ``lake`` when it holds the lake for
    writing; otherwise the lake is opened read-only (and a writer in
    another process makes this raise :class:`BackupError`). Missing
    stores are skipped and recorded as ``null`` in the manifest."""
    now = (now or datetime.now(UTC)).astimezone(UTC)
    dest_root = Path(dest_root)
    if not dest_root.exists():
        dest_root.mkdir(parents=True)
        _owner_only(dest_root, 0o700)
    backup_id = _new_id(dest_root, now)
    staging = dest_root / f"{backup_id}{PARTIAL_SUFFIX}"
    staging.mkdir(mode=0o700)
    _owner_only(staging, 0o700)
    log = _log.bind(backup_id=backup_id)
    try:
        state = None
        if paths.state.exists():
            state = _backup_state(paths.state, staging / STATE_FILE)
        lake_facts = None
        if lake is not None or paths.lake.exists():
            lake_facts = _backup_lake(paths, staging / LAKE_FILE, lake)
        artifacts = None
        if paths.artifacts.is_dir():
            shutil.copytree(
                paths.artifacts, staging / ARTIFACTS, ignore=shutil.ignore_patterns("*.tmp")
            )
            artifacts = {
                "files": sum(1 for p in (staging / ARTIFACTS).rglob("*") if p.is_file()),
                "unreadable_json": _settle_json(paths.artifacts, staging / ARTIFACTS),
            }
            if artifacts["unreadable_json"]:
                log.warning("backup.artifacts.unreadable_json", files=artifacts["unreadable_json"])
        manifest = {
            "format": FORMAT_VERSION,
            "id": backup_id,
            "created_at": now.isoformat(),
            "stonks_version": _stonks_version(),
            "git_sha": _git_sha(),
            "host": platform.node(),
            "sources": {
                "lake": str(paths.lake),
                "state": str(paths.state),
                "artifacts": str(paths.artifacts),
            },
            "state": state,
            "lake": lake_facts,
            "artifacts": artifacts,
            "files": _file_entries(staging),
        }
        (staging / MANIFEST).write_text(json.dumps(manifest, indent=2), encoding="utf-8")
        _restrict_tree(staging)
        final = dest_root / backup_id
        staging.rename(final)
    except BaseException:
        shutil.rmtree(staging, ignore_errors=True)
        raise
    log.info(
        "backup.created",
        path=str(final),
        files=len(manifest["files"]),
        bytes=sum(f["size"] for f in manifest["files"]),
    )
    return final


def _backup_state(src: Path, dst: Path) -> dict[str, Any]:
    dst.parent.mkdir(parents=True, exist_ok=True)
    source = sqlite3.connect(src)
    try:
        target = sqlite3.connect(dst)
        try:
            source.backup(target)
            # One self-contained file: no -wal/-shm siblings to carry.
            target.execute("PRAGMA journal_mode=DELETE")
        finally:
            target.close()
    finally:
        source.close()
    return _state_facts(dst)


def _backup_lake(paths: DataPaths, dst: Path, lake: DuckDBLake | None) -> dict[str, Any]:
    dst.parent.mkdir(parents=True, exist_ok=True)
    alias = f"stonks_backup_{uuid.uuid4().hex[:12]}"
    if lake is not None:
        # A cursor is a separate connection to the same database: its own
        # transaction, so the copy never joins one the owner has open.
        con = lake.con.cursor()
        source = con.execute("SELECT current_database()").fetchone()[0]
    else:
        con = duckdb.connect()
        source = f"stonks_src_{uuid.uuid4().hex[:12]}"
        try:
            con.execute(f"ATTACH {_lit(paths.lake)} AS {source} (READ_ONLY)")
        except duckdb.Error as exc:
            con.close()
            raise BackupError(
                f"the lake {paths.lake} is in use by another process or connection ({exc}). "
                "Stop the writer (stonks serve) or run the backup inside it (lake=...)."
            ) from exc
    try:
        con.execute(f"ATTACH {_lit(dst)} AS {alias}")
        try:
            # One statement, one transaction: a consistent snapshot.
            con.execute(f'COPY FROM DATABASE "{source}" TO {alias}')
            con.execute(f"CHECKPOINT {alias}")
            backend = _bar_backend(con, source)
        finally:
            con.execute(f"DETACH {alias}")
    finally:
        con.close()
    if backend == "parquet":
        reader = duckdb.connect()
        try:
            store = ParquetBarStore(paths.bars_root, reader, read_only=True)
            store.export_partitions(dst.parent / "bars")
        finally:
            reader.close()
    return _lake_facts(dst)


def _settle_json(
    src_root: Path, dst_root: Path, *, attempts: int = 3, delay: float = 0.2
) -> list[str]:
    """Re-copy JSON files that do not parse in the copy.

    The registry rewrites ``meta.json`` in place (status changes), so a
    copy can catch it half-written. Each unparsable copy is re-copied up to
    ``attempts`` times; files still unparsable (broken at the source too)
    are returned as relative paths for the manifest."""
    pending = [p for p in sorted(dst_root.rglob("*.json")) if not _parses(p)]
    for _ in range(attempts):
        if not pending:
            break
        time.sleep(delay)
        still = []
        for copy in pending:
            source = src_root / copy.relative_to(dst_root)
            with contextlib.suppress(OSError):
                shutil.copyfile(source, copy)
            if not _parses(copy):
                still.append(copy)
        pending = still
    return [p.relative_to(dst_root).as_posix() for p in pending]


def _parses(path: Path) -> bool:
    try:
        json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return False
    return True


def _bar_backend(con: duckdb.DuckDBPyConnection, database: str) -> str:
    has_settings = con.execute(
        "SELECT COUNT(*) FROM duckdb_tables() WHERE database_name = ? AND table_name = "
        "'lake_settings'",
        [database],
    ).fetchone()[0]
    if not has_settings:
        return "duckdb"
    row = con.execute(
        f"SELECT value FROM \"{database}\".lake_settings WHERE key = 'bars_backend'"
    ).fetchone()
    return row[0] if row else "duckdb"


# ---- facts, checksums, verify -------------------------------------------------------


def _state_facts(path: Path) -> dict[str, Any]:
    con = sqlite3.connect(f"{path.resolve().as_uri()}?mode=ro", uri=True)
    try:
        tables = [
            r[0]
            for r in con.execute(
                "SELECT name FROM sqlite_master WHERE type='table' "
                "AND name NOT LIKE 'sqlite_%' ORDER BY name"
            )
        ]
        counts = {t: con.execute(f'SELECT COUNT(*) FROM "{t}"').fetchone()[0] for t in tables}
        versions = (
            [r[0] for r in con.execute("SELECT version FROM schema_migrations ORDER BY version")]
            if "schema_migrations" in tables
            else []
        )
    finally:
        con.close()
    return {"schema_versions": versions, "row_counts": counts}


def _lake_facts(path: Path) -> dict[str, Any]:
    with DuckDBLake(path, read_only=True) as lake:
        tables = [
            r[0]
            for r in lake.con.execute(
                "SELECT table_name FROM duckdb_tables() "
                "WHERE database_name = current_database() AND schema_name = 'main' "
                "ORDER BY table_name"
            ).fetchall()
        ]
        counts = {
            t: int(lake.con.execute(f'SELECT COUNT(*) FROM main."{t}"').fetchone()[0])
            for t in tables
        }
        counts["bars"] = int(lake.con.execute("SELECT COUNT(*) FROM bars").fetchone()[0])
        versions = lake.applied_migrations() if "schema_migrations" in tables else []
        backend = lake.bar_backend
    return {"bar_backend": backend, "schema_versions": versions, "row_counts": counts}


def _file_entries(root: Path) -> list[dict[str, Any]]:
    out = []
    for path in sorted(p for p in root.rglob("*") if p.is_file()):
        rel = path.relative_to(root).as_posix()
        if rel == MANIFEST:
            continue
        out.append({"path": rel, "size": path.stat().st_size, "sha256": _sha256(path)})
    return out


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as fh:
        while chunk := fh.read(_CHUNK):
            digest.update(chunk)
    return digest.hexdigest()


def read_manifest(backup: str | Path) -> dict[str, Any]:
    path = Path(backup) / MANIFEST
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise BackupError(f"cannot read {path}: {exc}") from exc


def verify_backup(backup: str | Path) -> VerifyReport:
    """Check every file's size and SHA-256 against the manifest, then open
    the copies read-only (SQLite ``integrity_check``, DuckDB open) and
    compare schema versions and row counts. A component whose files fail
    the checksum is not opened."""
    backup = Path(backup)
    report = VerifyReport(backup)
    try:
        manifest = read_manifest(backup)
    except BackupError as exc:
        report.problems.append(str(exc))
        return report
    if manifest.get("format") != FORMAT_VERSION:
        report.problems.append(f"unknown backup format {manifest.get('format')!r}")
        return report
    bad: set[str] = set()
    listed = set()
    for entry in manifest.get("files", []):
        rel = entry["path"]
        listed.add(rel)
        path = backup / rel
        if not path.is_file():
            report.problems.append(f"{rel}: missing")
        elif path.stat().st_size != entry["size"]:
            report.problems.append(f"{rel}: size {path.stat().st_size} != {entry['size']}")
        elif _sha256(path) != entry["sha256"]:
            report.problems.append(f"{rel}: checksum mismatch")
        else:
            continue
        bad.add(rel.split("/", 1)[0])
    for path in backup.rglob("*"):
        rel = path.relative_to(backup).as_posix()
        if path.is_file() and rel != MANIFEST and rel not in listed:
            report.problems.append(f"{rel}: not in the manifest")
    if manifest.get("state") and "state" not in bad:
        _check_component(report, "state", manifest["state"], lambda: _state_check(backup))
    if manifest.get("lake") and "lake" not in bad:
        _check_component(report, "lake", manifest["lake"], lambda: _lake_facts(backup / LAKE_FILE))
    return report


def _state_check(backup: Path) -> dict[str, Any]:
    path = backup / STATE_FILE
    con = sqlite3.connect(f"{path.resolve().as_uri()}?mode=ro", uri=True)
    try:
        result = con.execute("PRAGMA integrity_check").fetchone()[0]
    finally:
        con.close()
    if result != "ok":
        raise BackupError(f"integrity_check: {result}")
    return _state_facts(path)


def _check_component(
    report: VerifyReport,
    name: str,
    expected: dict[str, Any],
    actual_facts: Callable[[], dict[str, Any]],
) -> None:
    try:
        actual = actual_facts()
    except Exception as exc:
        report.problems.append(f"{name}: cannot open the copy ({exc})")
        return
    if actual["schema_versions"] != expected["schema_versions"]:
        report.problems.append(
            f"{name}: schema versions {actual['schema_versions']} != {expected['schema_versions']}"
        )
    want, got = expected["row_counts"], actual["row_counts"]
    for table in sorted(set(want) | set(got)):
        if want.get(table) != got.get(table):
            report.problems.append(
                f"{name}.{table}: {got.get(table)} row(s), manifest says {want.get(table)}"
            )


# ---- retention and the scheduled entry point ------------------------------------------


def expired_backups(refs: Iterable[BackupRef], policy: BackupRetention) -> list[BackupRef]:
    """Backups the grandfather-father-son ``policy`` no longer keeps (see
    :class:`BackupRetention`), newest first."""
    ordered = sorted(refs, key=lambda r: r.created_at, reverse=True)
    if not ordered:
        return []
    keep = {ordered[0].id}
    buckets: tuple[tuple[int, Callable[[datetime], Any]], ...] = (
        (policy.daily, lambda t: t.date()),
        (policy.weekly, lambda t: tuple(t.isocalendar())[:2]),
        (policy.monthly, lambda t: (t.year, t.month)),
    )
    for count, bucket in buckets:
        seen: set[Any] = set()
        for ref in ordered:
            key = bucket(ref.created_at.astimezone(UTC))
            if key in seen:
                continue
            if len(seen) >= count:
                break
            seen.add(key)
            keep.add(ref.id)
    return [r for r in ordered if r.id not in keep]


@dataclass(frozen=True)
class BackupRunResult:
    ref: BackupRef
    pruned: list[str]


def run_backup(
    paths: DataPaths,
    target: BackupTarget,
    retention: BackupRetention | None,
    *,
    lake: DuckDBLake | None = None,
    now: datetime | None = None,
) -> BackupRunResult:
    """Create, verify and store one backup, then prune by ``retention``
    (``None`` keeps everything). What a scheduled backup job calls. A
    backup without the state DB or the lake is refused (and nothing is
    pruned): a restore needs both."""
    staging = target.staging_dir()
    tmp = None
    if staging is None:
        tmp = Path(tempfile.mkdtemp(prefix="stonks-backup-"))
        staging = tmp
    try:
        local = create_backup(paths, staging, lake=lake, now=now)
        report = verify_backup(local)
        if not report.ok:
            shutil.rmtree(local, ignore_errors=True)
            raise BackupError("new backup failed verification: " + "; ".join(report.problems))
        missing = _missing_stores(local, paths)
        if missing:
            # Stored, a backup of nothing would push the good backups out
            # of the retention buckets, one day at a time.
            shutil.rmtree(local, ignore_errors=True)
            raise BackupError(
                "nothing to back up: no " + " and no ".join(missing) + ". Check the data paths "
                "(STONKS_DATA_DIR); older backups were kept."
            )
        ref = target.put(local)
    finally:
        if tmp is not None:
            shutil.rmtree(tmp, ignore_errors=True)
    pruned: list[str] = []
    if retention is not None:
        for old in expired_backups(target.list(), retention):
            if old.id == ref.id:
                continue
            target.delete(old.id)
            pruned.append(old.id)
    _log.info("backup.stored", backup_id=ref.id, pruned=pruned)
    return BackupRunResult(ref=ref, pruned=pruned)


def _missing_stores(backup: Path, paths: DataPaths) -> list[str]:
    """The stores a backup lacks that a restore needs (the state DB and the
    lake), as text for the error."""
    manifest = read_manifest(backup)
    out = []
    if not manifest.get("state"):
        out.append(f"state DB at {paths.state}")
    if not manifest.get("lake"):
        out.append(f"lake at {paths.lake}")
    return out


def configured_target(settings: Any, dest: str | Path | None = None) -> LocalFilesystemTarget:
    """``dest``, else ``[backup].dir``, else a ``backups`` folder next to
    the lake: where every entrypoint (CLI, server job, scheduler) backs up."""
    root = dest or settings.backup.dir or Path(settings.lake.path).parent / "backups"
    return LocalFilesystemTarget(root)


def run_configured_backup(
    settings: Any,
    *,
    lake: DuckDBLake | None = None,
    dest: str | Path | None = None,
    prune: bool = True,
    now: datetime | None = None,
) -> BackupRunResult:
    """:func:`run_backup` of the configured stores to the configured target
    with the ``[backup]`` retention (``prune=False`` keeps everything).
    Pass ``lake`` when this process holds it (``stonks serve``)."""
    return run_backup(
        DataPaths.from_settings(settings),
        configured_target(settings, dest),
        settings.backup.retention if prune else None,
        lake=lake,
        now=now,
    )


# ---- helpers ----------------------------------------------------------------------------


def _owner_only(path: Path, mode: int) -> None:
    """``chmod`` where the OS has POSIX modes. On Windows it only sets or
    clears read-only, and 0600 and 0700 both keep the owner's write bit."""
    try:
        os.chmod(path, mode)
    except OSError as exc:  # a filesystem without modes (some mounts)
        _log.warning("backup.chmod_failed", path=str(path), error=str(exc))


def _restrict_tree(root: Path) -> None:
    """Owner-only modes on a backup: folders 0700, files 0600. It holds the
    state DB (users, sealed broker credentials) and the whole lake."""
    _owner_only(root, 0o700)
    for path in root.rglob("*"):
        _owner_only(path, 0o700 if path.is_dir() else 0o600)


def _new_id(root: Path, now: datetime) -> str:
    base = f"stonks-{now:%Y%m%dT%H%M%S}Z"
    candidate, n = base, 0
    while (root / candidate).exists() or (root / f"{candidate}{PARTIAL_SUFFIX}").exists():
        n += 1
        candidate = f"{base}-{n}"
    return candidate


def _ref(manifest: dict[str, Any]) -> BackupRef:
    try:
        return BackupRef(
            id=manifest["id"], created_at=datetime.fromisoformat(manifest["created_at"])
        )
    except (KeyError, TypeError, ValueError) as exc:
        raise BackupError(f"malformed manifest: {exc}") from exc


def _lit(path: Path) -> str:
    return "'" + Path(path).as_posix().replace("'", "''") + "'"


def _stonks_version() -> str | None:
    try:
        return metadata.version("stonks")
    except metadata.PackageNotFoundError:
        return None


def _git_sha() -> str | None:
    try:
        out = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=Path(__file__).parent,
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    sha = out.stdout.strip()
    return sha if out.returncode == 0 and re.fullmatch(r"[0-9a-f]{40}", sha) else None

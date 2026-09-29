"""Backups, verification and restore of lake + state + artifacts
(roadmap 12.4)."""

from __future__ import annotations

import contextlib
import json
import sqlite3
import threading
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pandas as pd
import pytest

from stonks.core.interval import Interval
from stonks.ops.backup import (
    BackupError,
    DataPaths,
    LocalFilesystemTarget,
    create_backup,
    run_backup,
    verify_backup,
)
from stonks.ops.config import BackupRetention
from stonks.ops.restore import RestoreError, restore_backup
from stonks.store.lake import DuckDBLake
from stonks.store.state import SqliteState


def _bars(ticker, n=30, start="2024-12-01"):
    days = pd.date_range(start, periods=n, freq="D")
    return pd.DataFrame(
        {
            "ticker": ticker,
            "timestamp": days,
            "open": 10.0,
            "high": 11.0,
            "low": 9.0,
            "close": [10.0 + i * 0.1 for i in range(n)],
            "adj_close": 10.0,
            "volume": 100,
        }
    )


def _seed(root: Path, *, backend="duckdb") -> DataPaths:
    paths = DataPaths(
        lake=root / "lake.duckdb", state=root / "state.sqlite", artifacts=root / "artifacts"
    )
    with DuckDBLake(paths.lake, bar_backend=backend) as lake:
        lake.migrate()
        lake.upsert_bars(_bars("AAA.US"), Interval.DAY_1)
        lake.upsert_bars(_bars("BBB.US", n=5), Interval.DAY_1)
        lake.open_ingest_run("fake", "prices")
    with SqliteState(paths.state) as state:
        state.migrate()
        state.execute(
            "INSERT INTO tick_runs (id, started_at, status) VALUES (?, ?, ?)",
            ["tick-1", "2026-01-01T00:00:00", "ok"],
        )
    (paths.artifacts / "strat-1" / "reports").mkdir(parents=True)
    (paths.artifacts / "strat-1" / "meta.json").write_text('{"id": "strat-1"}')
    (paths.artifacts / "strat-1" / "reports" / "oos.json").write_text("{}")
    return paths


def _fresh(root: Path) -> DataPaths:
    return DataPaths(
        lake=root / "lake.duckdb", state=root / "state.sqlite", artifacts=root / "artifacts"
    )


def _bar_count(lake_path: Path) -> int:
    with DuckDBLake(lake_path, read_only=True) as lake:
        return int(lake.sql("SELECT COUNT(*) AS n FROM bars").iloc[0, 0])


@pytest.mark.parametrize("backend", ["duckdb", "parquet"])
def test_backup_verify_restore_round_trip(tmp_path, backend):
    src = _seed(tmp_path / "data", backend=backend)
    backup = create_backup(src, tmp_path / "backups")

    manifest = json.loads((backup / "manifest.json").read_text())
    assert manifest["lake"]["bar_backend"] == backend
    assert manifest["lake"]["row_counts"]["bars"] == 35
    assert manifest["lake"]["schema_versions"][-1] >= 13
    assert manifest["state"]["row_counts"]["tick_runs"] == 1
    assert any(f["path"] == "artifacts/strat-1/meta.json" for f in manifest["files"])
    assert all(len(f["sha256"]) == 64 for f in manifest["files"])
    assert "git_sha" in manifest
    assert verify_backup(backup).ok

    dst = _fresh(tmp_path / "restored")
    result = restore_backup(backup, dst)
    assert result.moved_aside == []
    assert _bar_count(dst.lake) == 35
    with DuckDBLake(dst.lake) as lake:
        assert lake.bar_backend == backend
        # The ingest_runs sequence survives: no id collision on the next run.
        assert lake.open_ingest_run("fake", "prices") == 2
    with SqliteState(dst.state) as state:
        assert state.count_rows("tick_runs") == 1
    assert (dst.artifacts / "strat-1" / "reports" / "oos.json").exists()


def test_backup_of_a_live_lake_uses_its_connection(tmp_path):
    src = _seed(tmp_path / "data")
    with DuckDBLake(src.lake) as live:
        # Another handle on the same file in this process conflicts, so the
        # backup must go through the open lake.
        with pytest.raises(BackupError, match="in use"):
            create_backup(src, tmp_path / "b1")
        backup = create_backup(src, tmp_path / "b2", lake=live)
        live.upsert_bars(_bars("CCC.US", n=3), Interval.DAY_1)
    assert verify_backup(backup).ok
    dst = _fresh(tmp_path / "restored")
    restore_backup(backup, dst)
    assert _bar_count(dst.lake) == 35


def test_duckdb_snapshot_is_transactionally_consistent_under_writes(tmp_path):
    src = _seed(tmp_path / "data")
    with DuckDBLake(src.lake) as live:
        live.con.execute("CREATE TABLE pairs (k INTEGER)")
        stop = threading.Event()

        def writer():
            cur = live.con.cursor()
            k = 0
            while not stop.is_set():
                cur.execute("BEGIN")
                cur.execute("INSERT INTO pairs VALUES (?), (?)", [k, k])
                cur.execute("COMMIT")
                k += 1

        t = threading.Thread(target=writer)
        t.start()
        try:
            backups = [create_backup(src, tmp_path / f"b{i}", lake=live) for i in range(3)]
        finally:
            stop.set()
            t.join()
    for backup in backups:
        with DuckDBLake(backup / "lake" / "lake.duckdb", read_only=True) as copy:
            n = int(copy.sql("SELECT COUNT(*) FROM pairs").iloc[0, 0])
        assert n % 2 == 0


def test_sqlite_snapshot_is_consistent_under_writes(tmp_path):
    src = _seed(tmp_path / "data")
    stop = threading.Event()

    def writer():
        con = sqlite3.connect(src.state, isolation_level=None)
        con.execute("PRAGMA journal_mode=WAL")
        con.execute("CREATE TABLE IF NOT EXISTS pairs (k INTEGER)")
        k = 0
        while not stop.is_set():
            con.execute("BEGIN IMMEDIATE")
            con.execute("INSERT INTO pairs VALUES (?)", [k])
            con.execute("INSERT INTO pairs VALUES (?)", [k])
            con.execute("COMMIT")
            k += 1
        con.close()

    # sqlite3's context manager commits but does not close (TT-14).
    with contextlib.closing(sqlite3.connect(src.state)) as con, con:
        con.execute("CREATE TABLE IF NOT EXISTS pairs (k INTEGER)")
    t = threading.Thread(target=writer)
    t.start()
    try:
        backups = [create_backup(src, tmp_path / f"b{i}") for i in range(3)]
    finally:
        stop.set()
        t.join()
    for backup in backups:
        assert verify_backup(backup).ok
        con = sqlite3.connect(backup / "state" / "state.sqlite")
        n = con.execute("SELECT COUNT(*) FROM pairs").fetchone()[0]
        con.close()
        assert n % 2 == 0


def test_parquet_bars_copied_whole_while_a_writer_rewrites(tmp_path):
    src = _seed(tmp_path / "data", backend="parquet")
    with DuckDBLake(src.lake) as live:
        stop = threading.Event()
        errors: list[BaseException] = []

        def writer():
            i = 0
            try:
                while not stop.is_set():
                    frame = _bars("AAA.US", n=800, start="2023-01-01")
                    frame["close"] = 10.0 + i
                    live.bar_store.upsert(frame.assign(interval="1d"))
                    i += 1
            except BaseException as exc:  # pragma: no cover - surfaced below
                errors.append(exc)

        t = threading.Thread(target=writer)
        t.start()
        try:
            backup = create_backup(src, tmp_path / "b", lake=live)
        finally:
            stop.set()
            t.join()
        assert not errors
    assert verify_backup(backup).ok
    # Every copied partition is a complete file with one close value
    # across the whole multi-year series.
    dst = _fresh(tmp_path / "restored")
    restore_backup(backup, dst)
    with DuckDBLake(dst.lake, read_only=True) as lake:
        closes = lake.sql(
            "SELECT DISTINCT close FROM bars WHERE ticker = 'AAA.US' "
            "AND timestamp >= '2023-01-01' AND timestamp < '2024-12-01'"
        )
    assert len(closes) <= 1


def test_verify_flags_corruption_and_missing_files(tmp_path):
    src = _seed(tmp_path / "data")
    backup = create_backup(src, tmp_path / "backups")
    meta = backup / "artifacts" / "strat-1" / "meta.json"
    meta.write_text('{"id": "tampered"}')
    (backup / "artifacts" / "strat-1" / "reports" / "oos.json").unlink()
    report = verify_backup(backup)
    assert not report.ok
    text = "\n".join(report.problems)
    assert "artifacts/strat-1/meta.json" in text
    assert "missing" in text


def test_verify_rechecks_row_counts(tmp_path):
    src = _seed(tmp_path / "data")
    backup = create_backup(src, tmp_path / "backups")
    manifest_path = backup / "manifest.json"
    manifest = json.loads(manifest_path.read_text())
    manifest["state"]["row_counts"]["tick_runs"] = 7
    manifest_path.write_text(json.dumps(manifest))
    report = verify_backup(backup)
    assert not report.ok
    assert any("tick_runs" in p for p in report.problems)


def test_restore_refuses_a_non_empty_target(tmp_path):
    src = _seed(tmp_path / "data")
    backup = create_backup(src, tmp_path / "backups")
    target = _seed(tmp_path / "target")
    with DuckDBLake(target.lake) as lake:
        lake.upsert_bars(_bars("ZZZ.US", n=2), Interval.DAY_1)
    before = _bar_count(target.lake)

    with pytest.raises(RestoreError, match="not empty"):
        restore_backup(backup, target)
    assert _bar_count(target.lake) == before  # untouched


def test_restore_force_moves_existing_data_aside(tmp_path):
    src = _seed(tmp_path / "data")
    backup = create_backup(src, tmp_path / "backups")
    target = _seed(tmp_path / "target")
    with DuckDBLake(target.lake) as lake:
        lake.upsert_bars(_bars("ZZZ.US", n=2), Interval.DAY_1)

    result = restore_backup(backup, target, force=True)
    assert _bar_count(target.lake) == 35
    assert result.moved_aside
    aside_lake = next(p for p in result.moved_aside if p.name.startswith("lake.duckdb"))
    assert _bar_count(aside_lake) == 37  # the replaced data is kept, not deleted


def test_force_restore_keeps_wal_files_paired_with_their_database(tmp_path):
    src = _seed(tmp_path / "data")
    backup = create_backup(src, tmp_path / "backups")
    target = _seed(tmp_path / "target")
    (target.state.parent / "state.sqlite-wal").write_bytes(b"wal")
    (target.lake.parent / "lake.duckdb.wal").write_bytes(b"wal")
    result = restore_backup(backup, target, force=True)
    names = {p.name for p in result.moved_aside}
    [state_aside] = [n for n in names if n.startswith("state.sqlite.pre") and n.endswith("Z")]
    [lake_aside] = [n for n in names if n.startswith("lake.duckdb.pre") and n.endswith("Z")]
    # SQLite finds a WAL at "<db>-wal", DuckDB at "<db>.wal".
    assert f"{state_aside}-wal" in names
    assert f"{lake_aside}.wal" in names
    assert not (target.state.parent / "state.sqlite-wal").exists()


def test_force_restore_rolls_back_when_moving_aside_fails(tmp_path, monkeypatch):
    src = _seed(tmp_path / "data")
    backup = create_backup(src, tmp_path / "backups")
    target = _seed(tmp_path / "target")
    with DuckDBLake(target.lake) as lake:
        lake.upsert_bars(_bars("ZZZ.US", n=2), Interval.DAY_1)
    real_rename = Path.rename

    def rename(self, dst):
        if self.name == "state.sqlite":  # e.g. held open by a running process
            raise PermissionError("in use")
        return real_rename(self, dst)

    monkeypatch.setattr(Path, "rename", rename)
    with pytest.raises(RestoreError, match="in use"):
        restore_backup(backup, target, force=True)
    monkeypatch.setattr(Path, "rename", real_rename)
    assert _bar_count(target.lake) == 37
    # Data not yet moved aside when the failure hit is left alone.
    with SqliteState(target.state) as state:
        assert state.count_rows("tick_runs") == 1
    assert (target.artifacts / "strat-1" / "meta.json").exists()
    assert not list(target.lake.parent.glob("*.pre-restore-*"))


def test_force_restore_rolls_back_when_copying_fails(tmp_path, monkeypatch):
    import stonks.ops.restore as restore_module

    src = _seed(tmp_path / "data", backend="parquet")
    backup = create_backup(src, tmp_path / "backups")
    target = _seed(tmp_path / "target")
    with DuckDBLake(target.lake) as lake:
        lake.upsert_bars(_bars("ZZZ.US", n=2), Interval.DAY_1)

    def boom(src_dir, dst_dir):
        raise OSError("disk full")

    monkeypatch.setattr(restore_module, "_place_tree", boom)
    with pytest.raises(RestoreError, match="disk full"):
        restore_backup(backup, target, force=True)
    assert _bar_count(target.lake) == 37
    assert (target.artifacts / "strat-1" / "meta.json").exists()
    assert not list(target.lake.parent.glob("*.pre-restore-*"))
    assert not list(target.lake.parent.glob("*.restoring"))


def test_artifact_json_torn_during_copy_is_recopied(tmp_path):
    from stonks.ops.backup import _settle_json

    src = tmp_path / "src"
    dst = tmp_path / "dst"
    (src / "s1").mkdir(parents=True)
    (dst / "s1").mkdir(parents=True)
    (src / "s1" / "meta.json").write_text('{"status": "active"}')
    (dst / "s1" / "meta.json").write_text('{"status": "ac')  # torn mid-write
    assert _settle_json(src, dst) == []
    assert json.loads((dst / "s1" / "meta.json").read_text()) == {"status": "active"}
    (src / "s1" / "meta.json").write_text("not json")
    (dst / "s1" / "meta.json").write_text("not json")
    assert _settle_json(src, dst, attempts=2, delay=0) == ["s1/meta.json"]


def test_unreadable_artifact_json_is_recorded_not_fatal(tmp_path):
    src = _seed(tmp_path / "data")
    (src.artifacts / "strat-1" / "params.json").write_text("{broken")
    backup = create_backup(src, tmp_path / "backups")
    manifest = json.loads((backup / "manifest.json").read_text())
    assert manifest["artifacts"]["unreadable_json"] == ["strat-1/params.json"]


def test_restore_refuses_a_corrupt_backup(tmp_path):
    src = _seed(tmp_path / "data")
    backup = create_backup(src, tmp_path / "backups")
    with open(backup / "lake" / "lake.duckdb", "r+b") as fh:
        fh.seek(5000)
        fh.write(b"\x00garbage\x00")
    dst = _fresh(tmp_path / "restored")
    with pytest.raises(RestoreError, match="verification"):
        restore_backup(backup, dst)
    assert not dst.lake.exists()


def test_restore_refuses_backup_from_newer_schema(tmp_path):
    src = _seed(tmp_path / "data")
    backup = create_backup(src, tmp_path / "backups")
    with DuckDBLake(backup / "lake" / "lake.duckdb") as lake:
        lake.con.execute("INSERT INTO schema_migrations VALUES (999, now())")
    _refresh_manifest(backup)
    dst = _fresh(tmp_path / "restored")
    with pytest.raises(RestoreError, match="newer"):
        restore_backup(backup, dst)
    assert not dst.lake.exists()


def test_restore_runs_migrations_on_an_older_backup(tmp_path):
    src = _seed(tmp_path / "data")
    backup = create_backup(src, tmp_path / "backups")
    # Pretend the backup predates migration 013.
    lake_file = backup / "lake" / "lake.duckdb"
    with DuckDBLake(lake_file) as lake:
        lake.con.execute("DELETE FROM schema_migrations WHERE version = 13")
        lake.con.execute("DROP TABLE quarantined_bars")
        lake.con.execute("DROP SEQUENCE quarantined_bars_id_seq")
        lake.con.execute("ALTER TABLE ingest_runs DROP COLUMN quality_json")
    _refresh_manifest(backup)
    dst = _fresh(tmp_path / "restored")
    result = restore_backup(backup, dst)
    assert 13 in result.lake_migrations_applied
    with DuckDBLake(dst.lake, read_only=True) as lake:
        assert "quarantined_bars" in lake.tables()


def test_backup_skips_missing_components(tmp_path):
    root = tmp_path / "data"
    paths = _fresh(root)
    with SqliteState(paths.state) as state:
        state.migrate()
    backup = create_backup(paths, tmp_path / "backups")
    manifest = json.loads((backup / "manifest.json").read_text())
    assert manifest["lake"] is None
    assert manifest["artifacts"] is None
    assert verify_backup(backup).ok


def test_run_backup_puts_on_target_and_prunes(tmp_path):
    src = _seed(tmp_path / "data")
    target = LocalFilesystemTarget(tmp_path / "backups")
    start = datetime(2026, 9, 1, 3, tzinfo=UTC)
    for day in range(4):
        run_backup(
            src,
            target,
            BackupRetention(daily=2, weekly=0, monthly=0),
            now=start + timedelta(days=day),
        )
    refs = target.list()
    assert len(refs) == 2
    assert refs[0].created_at > refs[1].created_at
    # Unfinished staging folders are neither listed nor pruned away.
    (tmp_path / "backups" / "stonks-x.partial").mkdir()
    assert len(target.list()) == 2
    fetched = target.fetch(refs[0].id)
    assert verify_backup(fetched).ok


def _refresh_manifest(backup: Path) -> None:
    """Recompute checksums and counts after a test edits a backup file."""
    from stonks.ops.backup import _file_entries, _lake_facts

    manifest_path = backup / "manifest.json"
    manifest = json.loads(manifest_path.read_text())
    manifest["files"] = _file_entries(backup)
    manifest["lake"] = _lake_facts(backup / "lake" / "lake.duckdb")
    manifest_path.write_text(json.dumps(manifest))


def test_run_backup_refuses_a_backup_without_the_stores_and_keeps_the_old_ones(tmp_path):
    """A scheduled backup pointed at the wrong folder (no state DB, no lake)
    must not count as a backup: stored, it would push the good ones out of
    the retention buckets day after day until none is left."""
    src = _seed(tmp_path / "data")
    target = LocalFilesystemTarget(tmp_path / "backups")
    retention = BackupRetention(daily=1, weekly=0, monthly=0)
    start = datetime(2026, 9, 1, 3, tzinfo=UTC)
    good = run_backup(src, target, retention, now=start).ref
    wrong = _fresh(tmp_path / "empty")
    with pytest.raises(BackupError, match="state DB"):
        run_backup(wrong, target, retention, now=start + timedelta(days=1))
    assert [r.id for r in target.list()] == [good.id]
    assert [p.name for p in (tmp_path / "backups").iterdir()] == [good.id]


def test_backup_folders_and_files_are_owner_only(tmp_path, monkeypatch):
    """A backup holds every secret the state DB seals and every user row:
    folders 0700 and files 0600 where the OS has POSIX modes (review wave 2).
    On Windows ``os.chmod`` only toggles read-only, so the calls are checked."""
    import os
    import stat

    from stonks.ops import backup as backup_mod

    calls: dict[Path, int] = {}
    real_chmod = os.chmod

    def spy(path, mode, *args, **kwargs):
        calls[Path(path)] = mode
        return real_chmod(path, mode, *args, **kwargs)

    monkeypatch.setattr(backup_mod.os, "chmod", spy)
    src = _seed(tmp_path / "data")
    root = tmp_path / "backups"
    backup = create_backup(src, root)

    files = [p for p in backup.rglob("*") if p.is_file()]
    folders = [backup, *(p for p in backup.rglob("*") if p.is_dir())]
    assert files and len(folders) > 1
    staged = {p.name for p in calls}
    for f in files:
        assert f.name in staged
    assert calls[root] == 0o700
    assert all(mode in (0o600, 0o700) for mode in calls.values())
    if os.name == "posix":
        assert stat.S_IMODE(root.stat().st_mode) == 0o700
        for d in folders:
            assert stat.S_IMODE(d.stat().st_mode) == 0o700, d
        for f in files:
            assert stat.S_IMODE(f.stat().st_mode) == 0o600, f

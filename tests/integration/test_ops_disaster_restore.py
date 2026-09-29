"""Disaster restore from an off-server snapshot (TO-01).

``deploy/backup/restore.sh`` and ``restore-test.sh`` call
``python -m stonks.ops restore-snapshot`` and ``check-restore``. These tests
run the same code against a temp folder laid out the way ``restic restore``
leaves it: either app backups under ``data/backups/stonks-<id>/`` or a
stopped-volume copy with the stores straight under ``data/``.
"""

from __future__ import annotations

import shutil
from datetime import UTC, datetime
from pathlib import Path

import pytest
from typer.testing import CliRunner

from stonks.core.interval import Interval
from stonks.ops.backup import DataPaths, create_backup
from stonks.ops.commands import app
from stonks.ops.disaster import (
    CHECKED_TABLES,
    DisasterRestoreError,
    check_restored,
    find_snapshot,
    restore_snapshot,
)
from stonks.store.lake import DuckDBLake
from stonks.store.state import SqliteState
from tests.integration.test_ops_backup import _bars

_NOW = "2026-01-02T00:00:00+00:00"


def _seed(root: Path) -> DataPaths:
    """A data folder with real rows in users, strategies and orders."""
    paths = DataPaths.under(root)
    with DuckDBLake(paths.lake) as lake:
        lake.migrate()
        lake.upsert_bars(_bars("AAA.US", n=6), Interval.DAY_1)
    with SqliteState(paths.state) as state:
        state.migrate()
        state.execute(
            "INSERT INTO users (id, kind, email, display_name, role, status, created_at) "
            "VALUES ('usr_t1', 'human', 't1@example.com', 'Trader', 'trader', 'active', ?)",
            [_NOW],
        )
        state.execute(
            "INSERT INTO strategies (id, class_path, params_json, artifact_path, status, "
            "created_at, updated_at) VALUES ('mom_1', 'x:Y', '{}', 'mom_1', 'shadow', ?, ?)",
            [_NOW, _NOW],
        )
        state.execute(
            "INSERT INTO tick_runs (id, started_at, status) VALUES ('tick-1', ?, 'ok')", [_NOW]
        )
        for i in range(3):
            state.execute(
                "INSERT INTO orders (client_id, tick_id, strategy_id, ticker, side, quantity, "
                "order_type, status, created_at, updated_at) "
                "VALUES (?, 'tick-1', 'mom_1', 'AAA.US', 'buy', 1, 'market', 'filled', ?, ?)",
                [f"c{i}", _NOW, _NOW],
            )
    (paths.artifacts / "mom_1").mkdir(parents=True)
    (paths.artifacts / "mom_1" / "params.json").write_text("{}")
    return paths


def _counts(state_path: Path) -> dict[str, int]:
    with SqliteState(state_path) as state:
        return {t: state.count_rows(t) for t in CHECKED_TABLES}


def _restic_tree_with_app_backups(tmp_path: Path) -> tuple[Path, Path]:
    """``restic restore`` of an app-backup snapshot: an older and a newer
    backup under ``data/backups``. Returns (restore root, newest backup)."""
    src = _seed(tmp_path / "live")
    backups = tmp_path / "restore" / "data" / "backups"
    create_backup(src, backups, now=datetime(2026, 1, 1, tzinfo=UTC))
    with SqliteState(src.state) as state:
        state.execute(
            "INSERT INTO orders (client_id, tick_id, strategy_id, ticker, side, quantity, "
            "order_type, status, created_at, updated_at) "
            "VALUES ('c9', 'tick-1', 'mom_1', 'AAA.US', 'sell', 1, 'market', 'filled', ?, ?)",
            [_NOW, _NOW],
        )
    newest = create_backup(src, backups, now=datetime(2026, 1, 2, tzinfo=UTC))
    return tmp_path / "restore", newest


def _restic_tree_with_volume_copy(tmp_path: Path) -> Path:
    root = tmp_path / "restore"
    _seed(root / "data")
    return root


def _occupied_target(root: Path) -> DataPaths:
    """A data folder that already holds something (a half-set-up host)."""
    root.mkdir(parents=True)
    (root / "lake.duckdb").write_text("old lake")
    (root / "state.sqlite").write_text("old state")
    (root / "artifacts").mkdir()
    (root / "artifacts" / "old").write_text("x")
    return DataPaths.under(root)


# ---- finding the snapshot ---------------------------------------------------


def test_find_snapshot_picks_the_newest_app_backup(tmp_path):
    root, newest = _restic_tree_with_app_backups(tmp_path)
    snap = find_snapshot(root)
    assert snap.kind == "app_backup"
    assert snap.path == newest


def test_find_snapshot_orders_same_second_backups_by_counter(tmp_path):
    """Backups made in one second get ``-1`` .. ``-10``: by name ``-10``
    sorts before ``-9``, so the counter is compared as a number."""
    backups = tmp_path / "restore" / "data" / "backups"
    for suffix in ("", "-1", "-9", "-10"):
        folder = backups / f"stonks-20260102T000000Z{suffix}"
        folder.mkdir(parents=True)
        (folder / "manifest.json").write_text("{}", encoding="utf-8")
    older = backups / "stonks-20260101T235959Z-99"
    older.mkdir()
    (older / "manifest.json").write_text("{}", encoding="utf-8")
    snap = find_snapshot(tmp_path / "restore")
    assert snap.path.name == "stonks-20260102T000000Z-10"


def test_find_snapshot_falls_back_to_a_stopped_volume_copy(tmp_path):
    root = _restic_tree_with_volume_copy(tmp_path)
    snap = find_snapshot(root)
    assert snap.kind == "volume"
    assert snap.path == root / "data"


def test_find_snapshot_with_no_lake_or_state_is_refused(tmp_path):
    (tmp_path / "restore" / "data").mkdir(parents=True)
    with pytest.raises(DisasterRestoreError, match="no Stonks data"):
        find_snapshot(tmp_path / "restore")


# ---- restoring --------------------------------------------------------------


def test_app_backup_restores_state_artifacts_and_lake_together(tmp_path):
    root, newest = _restic_tree_with_app_backups(tmp_path)
    target = _occupied_target(tmp_path / "data")

    result = restore_snapshot(root, tmp_path / "data")

    assert _counts(target.state) == {"users": 2, "strategies": 1, "orders": 4}
    assert (target.artifacts / "mom_1" / "params.json").exists()
    with DuckDBLake(target.lake, read_only=True) as lake:
        assert lake.count_rows("bars") == 6
    # What was there is moved aside, never deleted.
    assert result.moved_aside
    assert all(p.exists() for p in result.moved_aside)
    assert check_restored(tmp_path / "data", result.expected) == []


def test_volume_copy_restores_state_artifacts_and_lake_together(tmp_path):
    root = _restic_tree_with_volume_copy(tmp_path)
    target = _occupied_target(tmp_path / "data")

    result = restore_snapshot(root, tmp_path / "data")

    assert _counts(target.state) == {"users": 2, "strategies": 1, "orders": 3}
    assert (target.artifacts / "mom_1" / "params.json").exists()
    with DuckDBLake(target.lake, read_only=True) as lake:
        assert lake.count_rows("bars") == 6
    assert all(p.exists() for p in result.moved_aside)
    assert result.expected == {"users": 2, "strategies": 1, "orders": 3}
    assert check_restored(tmp_path / "data", result.expected) == []


def test_snapshot_without_state_is_refused_and_the_target_is_untouched(tmp_path):
    root = _restic_tree_with_volume_copy(tmp_path)
    (root / "data" / "state.sqlite").unlink()
    target = _occupied_target(tmp_path / "data")

    with pytest.raises(DisasterRestoreError, match="state"):
        restore_snapshot(root, tmp_path / "data")

    assert target.state.read_text() == "old state"
    assert target.lake.read_text() == "old lake"
    assert (target.artifacts / "old").exists()


def test_app_backup_without_state_is_refused(tmp_path):
    src = _seed(tmp_path / "live")
    src.state.unlink()
    create_backup(src, tmp_path / "restore" / "data" / "backups")
    target = _occupied_target(tmp_path / "data")

    with pytest.raises(DisasterRestoreError, match="state"):
        restore_snapshot(tmp_path / "restore", tmp_path / "data")
    assert target.state.read_text() == "old state"


def test_snapshot_with_strategies_but_no_artifacts_is_refused(tmp_path):
    root = _restic_tree_with_volume_copy(tmp_path)
    shutil.rmtree(root / "data" / "artifacts")
    _occupied_target(tmp_path / "data")

    with pytest.raises(DisasterRestoreError, match="artifacts"):
        restore_snapshot(root, tmp_path / "data")
    assert (tmp_path / "data" / "artifacts" / "old").exists()


# ---- checking a restore -------------------------------------------------------


def test_check_fails_when_state_is_missing(tmp_path):
    (tmp_path / "data").mkdir()
    problems = check_restored(tmp_path / "data", {"users": 1, "strategies": 0, "orders": 0})
    assert any("state.sqlite" in p and "missing" in p for p in problems)


def test_check_fails_when_state_is_empty(tmp_path):
    (tmp_path / "data").mkdir()
    (tmp_path / "data" / "state.sqlite").write_bytes(b"")
    problems = check_restored(tmp_path / "data", {"users": 1})
    assert any("empty" in p for p in problems)


def test_check_fails_on_a_fresh_state_that_lost_the_rows(tmp_path):
    """The old restore test passed on a freshly migrated state DB."""
    with SqliteState(tmp_path / "data" / "state.sqlite") as state:
        state.migrate()
    problems = check_restored(tmp_path / "data", {"users": 2, "strategies": 1, "orders": 3})
    assert any("strategies" in p for p in problems)
    assert any("orders" in p for p in problems)
    assert any("users" in p for p in problems)


# ---- the CLI the shell scripts call --------------------------------------------


def test_cli_restore_snapshot_then_check_restore(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    root, _ = _restic_tree_with_app_backups(tmp_path)
    runner = CliRunner()

    r = runner.invoke(app, ["restore-snapshot", str(root), "--data-dir", str(tmp_path / "data")])
    assert r.exit_code == 0, r.output
    assert "orders=4" in r.output

    r = runner.invoke(app, ["check-restore", str(tmp_path / "data"), "--snapshot", str(root)])
    assert r.exit_code == 0, r.output


def test_cli_check_restore_fails_on_missing_state(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    root, _ = _restic_tree_with_app_backups(tmp_path)
    (tmp_path / "data").mkdir()
    r = CliRunner().invoke(app, ["check-restore", str(tmp_path / "data"), "--snapshot", str(root)])
    assert r.exit_code == 1
    assert "missing" in r.output


# ---- the shell scripts use this code ------------------------------------------

_BACKUP_DIR = Path(__file__).resolve().parents[2] / "deploy" / "backup"


def test_restore_script_restores_through_the_app_and_never_wipes_data():
    script = (_BACKUP_DIR / "restore.sh").read_text(encoding="utf-8")
    assert "stonks.ops restore-snapshot /restore/snapshot --data-dir /data" in script
    assert "rm -rf {}" not in script  # the old "wipe /data, copy the lake" step


def test_restore_test_script_checks_the_state_rows():
    script = (_BACKUP_DIR / "restore-test.sh").read_text(encoding="utf-8")
    assert "restore-snapshot /restore/snapshot --data-dir /restore/check" in script
    assert "check-restore /restore/check --snapshot /restore/snapshot" in script
    assert "db info" not in script

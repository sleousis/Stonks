"""``python -m stonks.ops backup | verify | restore | list | prune``."""

from __future__ import annotations

import os
import subprocess
import sys

import pytest
from typer.testing import CliRunner

from stonks.core.interval import Interval
from stonks.ops.commands import app
from stonks.store.lake import DuckDBLake
from stonks.store.state import SqliteState
from tests.integration.test_ops_backup import _bars


@pytest.fixture
def data_dir(tmp_path, monkeypatch):
    root = tmp_path / "data"
    with DuckDBLake(root / "lake.duckdb") as lake:
        lake.migrate()
        lake.upsert_bars(_bars("AAA.US", n=4), Interval.DAY_1)
    with SqliteState(root / "state.sqlite") as state:
        state.migrate()
    (root / "artifacts").mkdir()
    monkeypatch.setenv("STONKS_DATA_DIR", str(root))
    monkeypatch.chdir(tmp_path)  # no config/default.toml here
    return root


def _run(*args):
    return CliRunner().invoke(app, list(args))


def test_backup_list_verify_restore(data_dir, tmp_path):
    r = _run("backup")
    assert r.exit_code == 0, r.output
    backups = data_dir / "backups"
    [backup] = [p for p in backups.iterdir() if p.is_dir()]
    assert backup.name in r.output

    r = _run("list")
    assert r.exit_code == 0 and backup.name in r.output

    r = _run("verify", backup.name)
    assert r.exit_code == 0 and "OK" in r.output

    # Restore into the live data dir is refused ...
    r = _run("restore", backup.name)
    assert r.exit_code == 1
    assert "not empty" in r.output

    # ... an empty one works.
    target = tmp_path / "fresh"
    r = _run("restore", str(backup), "--data-dir", str(target))
    assert r.exit_code == 0, r.output
    with DuckDBLake(target / "lake.duckdb", read_only=True) as lake:
        assert lake.count_rows("bars") == 4


def test_verify_reports_problems_with_exit_code(data_dir):
    assert _run("backup").exit_code == 0
    [backup] = [p for p in (data_dir / "backups").iterdir() if p.is_dir()]
    (backup / "state" / "state.sqlite").write_bytes(b"broken")
    r = _run("verify", str(backup))
    assert r.exit_code == 1
    assert "state/state.sqlite" in r.output


def test_backup_dest_and_no_prune(data_dir, tmp_path):
    dest = tmp_path / "elsewhere"
    r = _run("backup", "--dest", str(dest), "--no-prune")
    assert r.exit_code == 0, r.output
    assert any(p.name.startswith("stonks-") for p in dest.iterdir())


def test_unknown_backup_id(data_dir):
    r = _run("verify", "stonks-20000101T000000Z")
    assert r.exit_code == 1
    assert "no backup" in r.output


def test_module_entry_point_help():
    out = subprocess.run(
        [sys.executable, "-m", "stonks.ops", "--help"],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        env={**os.environ, "PYTHONIOENCODING": "utf-8"},
        timeout=60,
    )
    assert out.returncode == 0
    for command in ("backup", "verify", "restore"):
        assert command in out.stdout

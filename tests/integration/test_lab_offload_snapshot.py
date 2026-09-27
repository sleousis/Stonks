"""Read-only lake snapshots for lab workers (roadmap 14.9)."""

from __future__ import annotations

import os
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta

import duckdb
import pytest

from stonks.lab.offload.snapshot import LakeSnapshots, lake_fingerprint
from tests.fixtures.parallel_lab import random_walk_lake

UNIVERSE = ["A.US", "B.US"]


class _Clock:
    def __init__(self) -> None:
        self.now = datetime(2026, 9, 27, 12, 0, tzinfo=UTC)

    def __call__(self) -> datetime:
        return self.now


@pytest.fixture(params=["duckdb", "parquet"])
def lake(request, tmp_path):
    lake = random_walk_lake(tmp_path / "data" / "lake.duckdb", UNIVERSE, periods=40)
    if request.param == "parquet":
        lake.migrate_bars_to_parquet()
    yield lake
    lake.close()


def _bar_count(lake) -> int:
    return int(lake.sql("SELECT COUNT(*) AS n FROM bars")["n"].iloc[0])


def test_publish_makes_a_read_only_copy_the_worker_can_open(lake, tmp_path):
    snaps = LakeSnapshots(tmp_path / "snaps")
    assert snaps.current() is None
    info = snaps.publish(lake)
    assert snaps.current() == info
    copy = info.open()
    try:
        assert copy.read_only
        assert copy.bar_backend == lake.bar_backend
        assert _bar_count(copy) == _bar_count(lake) > 0
        with pytest.raises(duckdb.Error):
            copy.con.execute("DELETE FROM instruments")
    finally:
        copy.close()


def test_a_snapshot_does_not_move_when_the_lake_changes(lake, tmp_path):
    snaps = LakeSnapshots(tmp_path / "snaps")
    info = snaps.publish(lake)
    before = _bar_count(lake)
    lake.con.execute("DELETE FROM instruments")
    copy = info.open()
    try:
        assert int(copy.sql("SELECT COUNT(*) AS n FROM instruments")["n"].iloc[0]) > 0
        assert _bar_count(copy) == before
    finally:
        copy.close()


def test_stale_when_older_than_the_max_age_or_the_lake_changed(lake, tmp_path):
    clock = _Clock()
    snaps = LakeSnapshots(tmp_path / "snaps", clock=clock)
    assert snaps.is_stale(None, lake, 60)
    info = snaps.publish(lake)
    assert not snaps.is_stale(info, lake, 60)
    clock.now += timedelta(minutes=61)
    assert snaps.is_stale(info, lake, 60)
    clock.now = info.created_at
    lake.con.execute(
        "INSERT INTO ingest_runs (source, kind, started_at, finished_at, status) "
        "VALUES ('fake', 'prices', now(), now(), 'ok')"
    )
    assert snaps.is_stale(info, lake, 60)


def test_ensure_fresh_reuses_a_fresh_snapshot_and_rebuilds_a_stale_one(lake, tmp_path):
    clock = _Clock()
    snaps = LakeSnapshots(tmp_path / "snaps", clock=clock)

    @contextmanager
    def opener():
        yield lake

    first = snaps.ensure_fresh(opener, 60)
    assert snaps.ensure_fresh(opener, 60) == first
    clock.now += timedelta(minutes=90)
    second = snaps.ensure_fresh(opener, 60)
    assert second.path != first.path
    assert snaps.current() == second


def test_fingerprint_changes_with_bar_fetches(lake):
    before = lake_fingerprint(lake)
    lake.con.execute(
        "INSERT INTO bar_fetch_ranges VALUES ('A.US', '1d', DATE '2026-01-01',"
        " DATE '2026-01-31', 'fake', 10, now())"
    )
    assert lake_fingerprint(lake) != before


def test_prune_keeps_the_newest_and_held_snapshots(lake, tmp_path):
    clock = _Clock()
    snaps = LakeSnapshots(tmp_path / "snaps", keep=1, clock=clock)
    first = snaps.publish(lake)
    with snaps.hold(first, "w1") as touch:
        touch()
        clock.now += timedelta(minutes=1)
        second = snaps.publish(lake)  # prunes, but first is held
        assert first.directory.exists()
    clock.now += timedelta(minutes=1)
    third = snaps.publish(lake)
    assert not first.directory.exists()
    assert not second.directory.exists()
    assert third.directory.exists()
    assert snaps.current() == third


def test_an_old_hold_does_not_block_pruning(lake, tmp_path):
    clock = _Clock()
    snaps = LakeSnapshots(tmp_path / "snaps", keep=1, clock=clock)
    first = snaps.publish(lake)
    mark = first.directory / "holds" / "dead-worker"
    mark.parent.mkdir()
    mark.touch()
    old = mark.stat().st_mtime - 7200
    os.utime(mark, (old, old))
    clock.now += timedelta(minutes=1)
    snaps.publish(lake)
    assert not first.directory.exists()


def test_current_ignores_a_missing_or_broken_marker(tmp_path):
    root = tmp_path / "snaps"
    root.mkdir()
    (root / "CURRENT.json").write_text("{not json", encoding="utf-8")
    assert LakeSnapshots(root).current() is None
    (root / "CURRENT.json").write_text(
        '{"dir": "gone", "created_at": "2026-01-01T00:00:00+00:00"}', encoding="utf-8"
    )
    assert LakeSnapshots(root).current() is None


# ---- universe changes make the snapshot stale (BE-19) ---------------------------------


def _membership_write(lake) -> None:
    import pandas as pd

    lake.upsert_universe_membership(
        pd.DataFrame([{"universe_id": "u1", "ticker": "A.US", "start_date": "2020-01-01"}])
    )


def _definition_write(lake) -> None:
    from stonks.universes import UniverseDefinition, UniverseStore

    UniverseStore(lake).save(UniverseDefinition(id="u1", kind="list", spec={"tickers": ["A.US"]}))


def _index_write(lake) -> None:
    from datetime import date

    from stonks.universes import UniverseStore
    from stonks.universes.base import IndexHistory

    UniverseStore(lake).save_index_history(
        IndexHistory(index_id="idx", as_of=date(2026, 1, 2), constituents=("A.US",))
    )


@pytest.mark.parametrize("write", [_membership_write, _definition_write, _index_write])
def test_a_universe_change_makes_the_snapshot_stale(lake, tmp_path, write):
    snaps = LakeSnapshots(tmp_path / "snaps")
    info = snaps.publish(lake)
    assert not snaps.is_stale(info, lake, 60)
    write(lake)
    assert snaps.is_stale(info, lake, 60)

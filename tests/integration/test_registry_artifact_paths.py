"""Artifact paths survive a move of the data folder (TO-09).

The registry stores each strategy's artifact path relative to its artifacts
folder, so a restore into a new data folder loads from the new folder.
Rows written before this change hold an absolute path; they resolve to the
same bundle under the current artifacts folder when it is there.
"""

from __future__ import annotations

import json
import shutil

from stonks.ops.backup import DataPaths, create_backup
from stonks.ops.restore import restore_backup
from stonks.registry.store import StrategyRegistry
from stonks.store.lake import DuckDBLake
from stonks.store.state import SqliteState
from stonks.strategies.examples.momentum import Momentum


def _momentum(lookback: int) -> Momentum:
    return Momentum({"lookback_days": lookback})


def test_register_stores_a_path_relative_to_the_artifacts_folder(tmp_path):
    with SqliteState(tmp_path / "state.sqlite") as state:
        state.migrate()
        reg = StrategyRegistry(state, tmp_path / "artifacts")
        sid = reg.register(_momentum(20), reports=[])
        (row,) = state.sql("SELECT artifact_path FROM strategies WHERE id = ?", [sid])
        assert row["artifact_path"] == sid
        [handle] = reg.list_all()
        assert handle.artifact_path == tmp_path / "artifacts" / sid


def test_restore_into_a_new_folder_loads_from_it_after_the_source_is_gone(tmp_path):
    src = DataPaths.under(tmp_path / "old")
    with DuckDBLake(src.lake) as lake:
        lake.migrate()
    with SqliteState(src.state) as state:
        state.migrate()
        sid = StrategyRegistry(state, src.artifacts).register(_momentum(33), reports=[])
    backup = create_backup(src, tmp_path / "backups")
    dst = DataPaths.under(tmp_path / "new")
    restore_backup(backup, dst)
    shutil.rmtree(tmp_path / "old")

    with SqliteState(dst.state) as state:
        loaded = StrategyRegistry(state, dst.artifacts).load(sid)
    assert loaded.params["lookback_days"] == 33


def test_a_legacy_absolute_path_resolves_under_the_current_artifacts_folder(tmp_path):
    with SqliteState(tmp_path / "state.sqlite") as state:
        state.migrate()
        reg = StrategyRegistry(state, tmp_path / "artifacts")
        sid = reg.register(_momentum(20), reports=[])
        # A row from before TO-09: the full path under an old data folder
        # that still exists with stale params.
        stale = tmp_path / "old-data" / "artifacts" / sid
        shutil.copytree(tmp_path / "artifacts" / sid, stale)
        params = json.loads((stale / "params.json").read_text())
        params["lookback_days"] = 99
        (stale / "params.json").write_text(json.dumps(params))
        state.execute("UPDATE strategies SET artifact_path = ? WHERE id = ?", [str(stale), sid])

        [handle] = reg.list_all()
        assert handle.artifact_path == tmp_path / "artifacts" / sid
        assert reg.load(sid).params["lookback_days"] == 20


def test_a_legacy_absolute_path_is_kept_when_the_bundle_is_not_in_the_artifacts_folder(
    tmp_path,
):
    with SqliteState(tmp_path / "state.sqlite") as state:
        state.migrate()
        reg = StrategyRegistry(state, tmp_path / "artifacts")
        sid = reg.register(_momentum(20), reports=[])
        elsewhere = tmp_path / "elsewhere" / sid
        shutil.move(tmp_path / "artifacts" / sid, elsewhere)
        state.execute("UPDATE strategies SET artifact_path = ? WHERE id = ?", [str(elsewhere), sid])

        [handle] = reg.list_all()
        assert handle.artifact_path == elsewhere
        assert reg.load(sid).params["lookback_days"] == 20

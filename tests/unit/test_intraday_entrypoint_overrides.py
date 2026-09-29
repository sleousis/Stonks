"""The intraday engine and the stream runner read the console overrides like
the CLI and the scheduler do (review wave 2, item 2), so a tightened risk
limit reaches the intraday books."""

from __future__ import annotations

import pytest

from stonks.config_overrides import OverrideStore
from stonks.store.state import SqliteState


@pytest.fixture
def overridden(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "config").mkdir()
    state_path = tmp_path / "state.sqlite"
    (tmp_path / "config" / "default.toml").write_text(
        f'[state]\npath = "{state_path.as_posix()}"\n'
        f'[lake]\npath = "{(tmp_path / "lake.duckdb").as_posix()}"\n'
    )
    with SqliteState(state_path) as state:
        state.migrate()
        OverrideStore(state).set(
            "production.risk.max_weight_per_ticker", 0.1, actor="user:ada", reason="tighter"
        )


@pytest.mark.parametrize("module", ["stonks.engine.__main__", "stonks.streaming.__main__"])
def test_the_entry_point_applies_the_console_overrides(overridden, module):
    import importlib

    settings = importlib.import_module(module)._settings()
    assert settings.production.risk.max_weight_per_ticker == 0.1

"""TO-06: the API process runs ticks as jobs, so its start closes tick
rows a killed process left at ``running``."""

from __future__ import annotations

from datetime import UTC, datetime

from stonks.store.state import SqliteState

NOW = datetime(2026, 4, 3, 21, 0, tzinfo=UTC)


def _running(state, tick_id: str, started: datetime) -> None:
    state.execute(
        "INSERT INTO tick_runs (id, started_at, status) VALUES (?, ?, 'running')",
        [tick_id, started.isoformat(timespec="seconds")],
    )


def _row(state, tick_id):
    return state.sql("SELECT * FROM tick_runs WHERE id = ?", [tick_id])[0]


def test_the_api_start_recovers_interrupted_ticks(settings, seeded, fake_source):
    from stonks.app.context import AppContext
    from stonks.app.services import Services

    with SqliteState(settings.state.path) as s:
        _running(s, "tick_dead", NOW)
    svc = Services.create(AppContext(settings, source_factory=lambda: fake_source))
    svc.start()
    try:
        with SqliteState(settings.state.path) as s:
            assert _row(s, "tick_dead")["status"] == "error"
    finally:
        svc.shutdown()

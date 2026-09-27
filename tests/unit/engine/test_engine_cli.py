"""``python -m stonks.engine`` (roadmap 21.2.5). Hermetic: a recording on
disk, a lake and a state DB in tmp_path, the simulated broker."""

from __future__ import annotations

import json

import pytest

from stonks.config import Settings
from stonks.engine import __main__ as cli
from stonks.engine.control import EngineControl
from stonks.engine.settings import EngineSettings, control_dir_for
from stonks.store.state import SqliteState
from tests.unit.engine.engine_fixtures import day_frame, record
from tests.unit.engine.minute_lake import DAYS, TICKERS, MinuteMomentum, minute_lake


@pytest.fixture
def settings(tmp_path) -> Settings:
    s = Settings()
    s.lake.path = tmp_path / "lake.duckdb"
    s.state.path = tmp_path / "state.sqlite"
    minute_lake(path=s.lake.path).close()
    with SqliteState(s.state.path) as state:
        state.migrate()
    s.engine = EngineSettings.model_validate(
        {
            "universe": list(TICKERS),
            "books": [{"id": "mm", "portfolio_id": "pf_default", "strategies": ["mm"]}],
        }
    )
    return s


def strategies():
    return {"mm": MinuteMomentum({"lookback": 10})}


def last_json(out: str) -> dict:
    return json.loads(out[out.rindex("\n{\n") + 1 :] if "\n{\n" in out else out[out.index("{") :])


def test_replay_trades_a_recording_through_the_engine(settings, tmp_path, capsys) -> None:
    rec = record(day_frame(DAYS[0]), tmp_path / "rec")
    code = cli.main(
        ["replay", str(rec), "--session", DAYS[0].isoformat()],
        settings=settings,
        strategies=strategies(),
    )
    assert code == 0
    summary = last_json(capsys.readouterr().out)
    assert summary["orders_sent"] > 20
    with SqliteState(settings.state.path) as state:
        rows = state.sql("SELECT status FROM engine_runs")
        assert [r["status"] for r in rows] == ["stopped"]
        assert state.sql("SELECT count(*) AS n FROM orders")[0]["n"] == summary["orders_sent"]
    assert not EngineControl(control_dir_for(settings.engine, settings.state.path)).running()


def test_run_is_refused_while_the_engine_is_off(settings, capsys) -> None:
    assert cli.main(["run"], settings=settings) == 2
    assert "off" in capsys.readouterr().err


def test_a_second_engine_is_refused(settings, tmp_path, capsys) -> None:
    control = EngineControl(control_dir_for(settings.engine, settings.state.path))
    lock = control.acquire()
    try:
        code = cli.main(["replay", str(tmp_path)], settings=settings, strategies=strategies())
    finally:
        lock.release()
    assert code == 3


def test_no_books_is_a_config_error(settings, tmp_path) -> None:
    settings.engine = EngineSettings()
    assert cli.main(["replay", str(tmp_path)], settings=settings) == 2


def test_run_on_a_closed_day_does_nothing(settings, capsys) -> None:
    settings.engine.enabled = True
    assert cli.main(["run", "--session", "2026-09-26"], settings=settings) == 0
    assert "not a session" in capsys.readouterr().out


def test_status_and_stop(settings, tmp_path, capsys) -> None:
    rec = record(day_frame(DAYS[0]), tmp_path / "rec")
    cli.main(["replay", str(rec), "--session", DAYS[0].isoformat()], settings=settings,
             strategies=strategies())  # fmt: skip
    capsys.readouterr()
    assert cli.main(["status"], settings=settings) == 0
    status = json.loads(capsys.readouterr().out)
    assert status["running"] is False
    assert status["latest"]["status"] == "stopped"
    assert status["latest"]["mode"] == "replay"
    assert cli.main(["stop"], settings=settings) == 0
    assert "no engine" in capsys.readouterr().out


def test_stop_times_out_while_the_engine_holds_on(settings, capsys) -> None:
    control = EngineControl(control_dir_for(settings.engine, settings.state.path))
    lock = control.acquire()
    try:
        assert cli.main(["stop", "--timeout", "0"], settings=settings) == 1
        assert control.stop_requested()
    finally:
        lock.release()

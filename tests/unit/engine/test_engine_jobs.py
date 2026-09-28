"""The ``engine_start`` and ``engine_stop`` scheduler jobs on all three
backends (roadmap 21.2.5). The launcher is a fake: nothing is spawned."""

from __future__ import annotations

from datetime import UTC, date, datetime

import pytest

from stonks.config import Settings
from stonks.engine.control import EngineControl, LaunchResult
from stonks.engine.settings import EngineSettings, control_dir_for
from stonks.notify import Notifier
from stonks.scheduling import jobs as jobs_mod
from stonks.scheduling.api_backend import API_ACTIONS
from stonks.scheduling.in_process import IN_PROCESS_ACTIONS
from stonks.scheduling.jobs import JobSpec, RunContext
from stonks.scheduling.local import LOCAL_ACTIONS
from stonks.scheduling.triggers import Fire, SessionTrigger

REGISTRIES = [LOCAL_ACTIONS, API_ACTIONS, IN_PROCESS_ACTIONS]
SESSION = date(2026, 9, 28)  # a Monday


class _Quiet(Notifier):
    def _send(self, n):  # pragma: no cover - never called here
        pass


class FakeLauncher:
    def __init__(self) -> None:
        self.calls: list[tuple[EngineControl, date]] = []

    def launch(self, control: EngineControl, session: date) -> LaunchResult:
        self.calls.append((control, session))
        return LaunchResult(pid=99, command=("engine",))


@pytest.fixture
def launcher(monkeypatch) -> FakeLauncher:
    fake = FakeLauncher()
    monkeypatch.setattr(jobs_mod, "engine_launcher", lambda: fake)
    return fake


@pytest.fixture
def settings(tmp_path) -> Settings:
    s = Settings()
    s.state.path = tmp_path / "state.sqlite"
    s.engine = EngineSettings.model_validate(
        {
            "enabled": True,
            "universe": ["AAA.US"],
            "books": [{"id": "b", "portfolio_id": "pf_default", "strategies": ["momentum"]}],
            "stop_timeout_seconds": 0.2,
        }
    )
    return s


def ctx(settings: Settings, action: str, day: date = SESSION) -> RunContext:
    at = datetime(day.year, day.month, day.day, 13, 15, tzinfo=UTC)
    return RunContext(
        spec=JobSpec(action, action, SessionTrigger("XNYS", "open")),
        fire=Fire(at, day, day.isoformat()),
        run_id="srun_e",
        now=at,
        settings=settings,
        notifier=_Quiet(),
    )


def control(settings: Settings) -> EngineControl:
    return EngineControl(control_dir_for(settings.engine, settings.state.path))


@pytest.mark.parametrize("registry", REGISTRIES, ids=lambda r: r.backend)
def test_start_launches_the_engine_for_the_session(registry, settings, launcher) -> None:
    out = registry.get("engine_start")(ctx(settings, "engine_start"))
    assert out.status == "succeeded"
    assert out.detail == {"pid": 99, "session": SESSION.isoformat()}
    assert launcher.calls[0][1] == SESSION
    assert launcher.calls[0][0].dir == control(settings).dir


@pytest.mark.parametrize("registry", REGISTRIES, ids=lambda r: r.backend)
def test_start_skips_while_the_engine_is_off(registry, settings, launcher) -> None:
    settings.engine.enabled = False
    out = registry.get("engine_start")(ctx(settings, "engine_start"))
    assert (out.status, out.detail["reason"]) == ("skipped", "engine_off")
    assert not launcher.calls


def test_start_skips_without_books_or_on_a_closed_day(settings, launcher) -> None:
    closed = LOCAL_ACTIONS.get("engine_start")(ctx(settings, "engine_start", date(2026, 9, 27)))
    assert closed.detail["reason"] == "market_closed"
    settings.engine.books = []
    none = LOCAL_ACTIONS.get("engine_start")(ctx(settings, "engine_start"))
    assert none.detail["reason"] == "no_books"
    assert not launcher.calls


def test_start_skips_when_an_engine_already_runs(settings, launcher) -> None:
    lock = control(settings).acquire()
    try:
        out = LOCAL_ACTIONS.get("engine_start")(ctx(settings, "engine_start"))
    finally:
        lock.release()
    assert out.detail["reason"] == "already_running"
    assert not launcher.calls


@pytest.mark.parametrize("registry", REGISTRIES, ids=lambda r: r.backend)
def test_stop_skips_when_nothing_runs(registry, settings) -> None:
    out = registry.get("engine_stop")(ctx(settings, "engine_stop"))
    assert (out.status, out.detail["reason"]) == ("skipped", "not_running")
    settings.engine.enabled = False
    out = registry.get("engine_stop")(ctx(settings, "engine_stop"))
    assert out.detail["reason"] == "engine_off"


def test_stop_asks_the_engine_to_stop_and_fails_when_it_hangs(settings) -> None:
    ctl = control(settings)
    lock = ctl.acquire()
    try:
        out = LOCAL_ACTIONS.get("engine_stop")(ctx(settings, "engine_stop"))
        assert ctl.stop_requested()
    finally:
        lock.release()
    assert out.status == "failed"
    assert out.detail["stopped"] is False


def test_stop_succeeds_once_the_engine_exits(settings, monkeypatch) -> None:
    ctl = control(settings)
    lock = ctl.acquire()

    def wait_stopped(self, timeout, **kw):
        lock.release()  # the engine saw the stop file and exited
        return True

    monkeypatch.setattr(EngineControl, "wait_stopped", wait_stopped)
    out = API_ACTIONS.get("engine_stop")(ctx(settings, "engine_stop"))
    assert out.status == "succeeded"

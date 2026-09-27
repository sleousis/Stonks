"""Engine control files and the launcher (roadmap 21.2.5)."""

from __future__ import annotations

import sys
from datetime import date
from typing import Any

import pytest

from stonks.engine.control import (
    EngineAlreadyRunningError,
    EngineControl,
    SubprocessLauncher,
    engine_command,
)
from stonks.engine.settings import EngineBookSettings, EngineSettings, control_dir_for


def test_the_lock_says_whether_an_engine_runs(tmp_path) -> None:
    control = EngineControl(tmp_path / "engine")
    assert not control.running()
    lock = control.acquire()
    try:
        assert control.running()
        with pytest.raises(EngineAlreadyRunningError):
            control.acquire()
    finally:
        lock.release()
    assert not control.running()


def test_a_new_start_clears_an_old_stop_request(tmp_path) -> None:
    control = EngineControl(tmp_path / "engine")
    control.request_stop("yesterday")
    assert control.stop_requested()
    lock = control.acquire()
    try:
        assert not control.stop_requested()
        control.request_stop("now")
        assert control.stop_requested()
    finally:
        lock.release()


def test_wait_stopped(tmp_path) -> None:
    control = EngineControl(tmp_path / "engine")
    assert control.wait_stopped(0.0)
    lock = control.acquire()
    ticks = iter(range(100))
    try:
        assert not control.wait_stopped(
            3.0, sleep=lambda _s: None, monotonic=lambda: float(next(ticks))
        )
    finally:
        lock.release()


class _Proc:
    pid = 4242


def test_the_launcher_runs_the_engine_module_detached(tmp_path) -> None:
    calls: list[tuple[list[str], dict[str, Any]]] = []

    def popen(cmd, **kw):
        calls.append((cmd, kw))
        return _Proc()

    control = EngineControl(tmp_path / "engine")
    result = SubprocessLauncher(popen=popen).launch(control, date(2026, 9, 28))
    assert result.pid == 4242
    cmd, kw = calls[0]
    assert cmd == [sys.executable, "-m", "stonks.engine", "run", "--session", "2026-09-28"]
    assert kw["stdout"] is not None and control.log_path.exists()
    assert "creationflags" in kw or kw.get("start_new_session") is True


def test_engine_command() -> None:
    assert engine_command(date(2026, 1, 2), python="py")[-2:] == ["--session", "2026-01-02"]


def test_settings_are_off_by_default_and_validate_books(tmp_path) -> None:
    cfg = EngineSettings()
    assert cfg.enabled is False and cfg.books == []
    assert control_dir_for(cfg, tmp_path / "state.sqlite") == tmp_path / "engine"
    assert control_dir_for(EngineSettings(control_dir=tmp_path / "x"), "s") == tmp_path / "x"
    b = {"id": "a", "portfolio_id": "pf_a", "strategies": ["momentum"]}
    with pytest.raises(ValueError, match="duplicate"):
        EngineSettings(books=[b, b])
    with pytest.raises(ValueError, match="share"):
        EngineSettings(books=[b, {**b, "id": "b"}])
    with pytest.raises(ValueError):
        EngineBookSettings(id="a", portfolio_id="pf", strategies=[])

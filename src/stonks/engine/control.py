"""Start and stop the engine process from outside (roadmap 21.2.5).

The engine is one always-on process per install. It is controlled through
files in its control directory (``[engine] control_dir``, default
``engine`` next to the state DB), so every scheduler backend and the
operator talk to it the same way:

- ``engine.lock``: the running process holds an OS file lock on it
  (``store.filelock``). The OS drops the lock when the process dies, so a
  crash never leaves a stale "running" flag. Nothing else is needed to
  know whether the engine is up.
- ``stop``: a stop request. The process checks it on every event (sources
  send heartbeats while idle) and stops cleanly.
- ``engine.log``: the output of a process the scheduler started.

:class:`EngineLauncher` is the seam that starts a process.
:class:`SubprocessLauncher` runs ``python -m stonks.engine run`` detached
from the scheduler, so a scheduler restart does not stop the engine.
Tests pass a fake launcher.
"""

from __future__ import annotations

import os
import subprocess
import sys
import time
from abc import ABC, abstractmethod
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import date
from pathlib import Path

from stonks.logging import get_logger
from stonks.store.filelock import FileLock

_log = get_logger("stonks.engine.control")

LOCK_NAME = "engine.lock"
STOP_NAME = "stop"
LOG_NAME = "engine.log"


class EngineAlreadyRunningError(RuntimeError):
    """Another engine process holds the lock."""


class EngineControl:
    """The control directory. See the module doc."""

    def __init__(self, directory: str | Path) -> None:
        self.dir = Path(directory)

    @property
    def lock_path(self) -> Path:
        return self.dir / LOCK_NAME

    @property
    def stop_path(self) -> Path:
        return self.dir / STOP_NAME

    @property
    def log_path(self) -> Path:
        return self.dir / LOG_NAME

    # ---- the process side ----------------------------------------------------------

    def acquire(self) -> FileLock:
        """Take the engine lock for this process. Raises
        :class:`EngineAlreadyRunningError` when another process holds it.
        Clears an old stop request, so a new start is not stopped at once."""
        lock = FileLock(self.lock_path, timeout=0.0)
        try:
            lock.acquire()
        except TimeoutError:
            raise EngineAlreadyRunningError(
                f"another engine process holds {self.lock_path}"
            ) from None
        self.clear_stop()
        return lock

    def stop_requested(self) -> bool:
        return self.stop_path.exists()

    def clear_stop(self) -> None:
        self.stop_path.unlink(missing_ok=True)

    # ---- the operator side ---------------------------------------------------------

    def running(self) -> bool:
        """Whether an engine process holds the lock now."""
        if not self.lock_path.exists():
            return False
        probe = FileLock(self.lock_path, timeout=0.0)
        try:
            probe.acquire()
        except TimeoutError:
            return True
        probe.release()
        return False

    def request_stop(self, reason: str = "requested") -> None:
        self.dir.mkdir(parents=True, exist_ok=True)
        self.stop_path.write_text(reason, encoding="utf-8")

    def wait_stopped(
        self,
        timeout: float,
        *,
        poll: float = 0.25,
        sleep: Callable[[float], object] = time.sleep,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> bool:
        """Wait up to ``timeout`` seconds for the process to exit."""
        deadline = monotonic() + timeout
        while self.running():
            if monotonic() >= deadline:
                return False
            sleep(poll)
        return True


@dataclass(frozen=True)
class LaunchResult:
    pid: int | None
    command: tuple[str, ...]


class EngineLauncher(ABC):
    """Starts an engine process for a session."""

    @abstractmethod
    def launch(self, control: EngineControl, session: date) -> LaunchResult: ...


def engine_command(session: date, *, python: str | None = None) -> list[str]:
    return [
        python or sys.executable,
        "-m",
        "stonks.engine",
        "run",
        "--session",
        session.isoformat(),
    ]


class SubprocessLauncher(EngineLauncher):
    """``python -m stonks.engine run`` in its own process group, output to
    ``engine.log``. The environment (``STONKS_CONFIG`` and the secrets) is
    passed on as is."""

    def __init__(
        self,
        *,
        python: str | None = None,
        popen: Callable[..., subprocess.Popen[bytes]] = subprocess.Popen,
        extra_args: Sequence[str] = (),
    ) -> None:
        self.python = python
        self._popen = popen
        self.extra_args = tuple(extra_args)

    def launch(self, control: EngineControl, session: date) -> LaunchResult:
        control.dir.mkdir(parents=True, exist_ok=True)
        command = [*engine_command(session, python=self.python), *self.extra_args]
        flags: dict[str, object] = {}
        if os.name == "nt":  # pragma: no cover - platform branch
            flags["creationflags"] = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0) | getattr(
                subprocess, "DETACHED_PROCESS", 0
            )
        else:  # pragma: no cover - platform branch
            flags["start_new_session"] = True
        with open(control.log_path, "ab") as log:
            proc = self._popen(
                command,
                stdin=subprocess.DEVNULL,
                stdout=log,
                stderr=subprocess.STDOUT,
                close_fds=True,
                **flags,
            )
        _log.info("engine.launched", pid=proc.pid, session=session.isoformat())
        return LaunchResult(pid=proc.pid, command=tuple(command))

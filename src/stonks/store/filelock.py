"""A small inter-process lock over a lock file.

The lock is an OS byte-range lock (``msvcrt.locking`` on Windows,
``fcntl.flock`` elsewhere) on an empty file, so the operating system drops
it when the holding process exits or crashes: a killed writer never leaves
a stale lock behind. The lock file itself stays on disk; deleting it while
another process might be waiting on it would break mutual exclusion.

Locks are per open handle, so two :class:`FileLock` objects on the same
path exclude each other inside one process as well.
"""

from __future__ import annotations

import os
import time
from pathlib import Path

if os.name == "nt":  # pragma: no cover - platform branch
    import msvcrt

    def _try_lock(fd: int) -> bool:
        try:
            msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
        except OSError:
            return False
        return True

    def _unlock(fd: int) -> None:
        os.lseek(fd, 0, os.SEEK_SET)
        msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)

else:  # pragma: no cover - platform branch
    import fcntl

    def _try_lock(fd: int) -> bool:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            return False
        return True

    def _unlock(fd: int) -> None:
        fcntl.flock(fd, fcntl.LOCK_UN)


class FileLock:
    """Exclusive lock on ``path`` (created with its parents if missing).

    ``acquire`` polls until the lock is free and raises ``TimeoutError``
    after ``timeout`` seconds. Use as a context manager."""

    def __init__(self, path: str | Path, *, timeout: float = 60.0, poll: float = 0.01) -> None:
        self.path = Path(path)
        self.timeout = timeout
        self.poll = poll
        self._fd: int | None = None

    def acquire(self) -> None:
        if self._fd is not None:
            raise RuntimeError(f"lock {self.path} is already held by this object")
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(self.path, os.O_RDWR | os.O_CREAT, 0o644)
        deadline = time.monotonic() + self.timeout
        delay = self.poll
        while not _try_lock(fd):
            if time.monotonic() >= deadline:
                os.close(fd)
                raise TimeoutError(f"could not lock {self.path} within {self.timeout}s")
            time.sleep(delay)
            delay = min(delay * 2, 0.2)
        self._fd = fd

    def release(self) -> None:
        fd, self._fd = self._fd, None
        if fd is None:
            return
        try:
            _unlock(fd)
        finally:
            os.close(fd)

    def __enter__(self) -> FileLock:
        self.acquire()
        return self

    def __exit__(self, *exc: object) -> None:
        self.release()

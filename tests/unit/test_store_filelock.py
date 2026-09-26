"""Inter-process writer lock over a lock file (``store.filelock``)."""

from __future__ import annotations

import pytest

from stonks.store.filelock import FileLock


def test_second_holder_times_out_until_the_first_releases(tmp_path):
    path = tmp_path / "locks" / "a.lock"
    first = FileLock(path, timeout=5)
    with first:
        assert path.exists()
        with pytest.raises(TimeoutError), FileLock(path, timeout=0.1):
            pass
    with FileLock(path, timeout=0.1):
        pass


def test_lock_is_released_when_the_block_raises(tmp_path):
    path = tmp_path / "b.lock"
    with pytest.raises(RuntimeError), FileLock(path, timeout=1):
        raise RuntimeError("boom")
    with FileLock(path, timeout=0.1):
        pass


def test_lock_is_not_reentrant_by_accident(tmp_path):
    lock = FileLock(tmp_path / "c.lock", timeout=1)
    with lock, pytest.raises(RuntimeError):
        lock.acquire()

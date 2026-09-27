"""The ``LabExecutor`` seam: where a heavy lab job runs (roadmap 14.9).

The job runner asks its executor about every job it is given:

- :class:`InProcessLabExecutor` (the default) keeps everything in the
  process that queued it, exactly as before;
- :class:`WorkerLabExecutor` leaves the configured kinds in the state DB
  queue for a lab worker (``python -m stonks.lab.offload worker``), after
  making sure a fresh read-only lake snapshot exists for it to read.

Either way the job is one ``jobs`` row with one result, so transports,
event streams and typed result routes do not change.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Callable, Mapping
from contextlib import AbstractContextManager
from pathlib import Path
from typing import Any

from stonks.lab.offload.queue import WORKER_EXECUTOR, LabQueue
from stonks.lab.offload.settings import LabOffloadSettings
from stonks.lab.offload.snapshot import LakeSnapshots
from stonks.store.lake import DuckDBLake

LakeOpener = Callable[[], AbstractContextManager[DuckDBLake]]


class LabExecutor(ABC):
    #: Stored in ``jobs.executor`` for jobs this executor takes.
    name: str

    @abstractmethod
    def offloads(self, kind: str, params: Mapping[str, Any]) -> bool:
        """True when a job of ``kind`` with ``params`` runs elsewhere."""

    def prepare(self, kind: str, params: Mapping[str, Any]) -> None:  # noqa: B027
        """Called before an offloaded job is queued (e.g. to publish data)."""

    def tracks(self, job_id: str) -> bool:
        """True while ``job_id`` waits for or runs on this executor."""
        return False

    def request_cancel(self, job_id: str) -> bool:
        """Ask a running offloaded job to stop; False when it is not one."""
        return False


class InProcessLabExecutor(LabExecutor):
    """Today's behaviour: every job runs in the process that queued it."""

    name = "local"

    def offloads(self, kind: str, params: Mapping[str, Any]) -> bool:
        return False


class WorkerLabExecutor(LabExecutor):
    """Queue :attr:`LabOffloadSettings.kinds` for a lab worker process.

    A lab run that fetches missing data first (``ensure_data``) stays in
    process: the fetch writes the lake, which only this process may do."""

    name = WORKER_EXECUTOR

    def __init__(
        self,
        settings: LabOffloadSettings,
        *,
        queue: LabQueue,
        snapshots: LakeSnapshots,
        open_lake: LakeOpener,
    ) -> None:
        self.settings = settings
        self.queue = queue
        self.snapshots = snapshots
        self._open_lake = open_lake

    def offloads(self, kind: str, params: Mapping[str, Any]) -> bool:
        return kind in self.settings.kinds and not params.get("ensure_data")

    def prepare(self, kind: str, params: Mapping[str, Any]) -> None:
        self.snapshots.ensure_fresh(self._open_lake, self.settings.snapshot_max_age_minutes)

    def tracks(self, job_id: str) -> bool:
        return self.queue.is_pending(job_id)

    def request_cancel(self, job_id: str) -> bool:
        return self.queue.request_cancel(job_id)


def make_lab_executor(
    settings: LabOffloadSettings,
    *,
    state_path: str | Path,
    lake_path: str | Path,
    open_lake: LakeOpener,
) -> LabExecutor:
    """The executor ``[lab.offload] executor`` names."""
    if settings.executor == "worker":
        return WorkerLabExecutor(
            settings,
            queue=LabQueue(state_path),
            snapshots=LakeSnapshots(
                settings.snapshot_root(lake_path), keep=settings.keep_snapshots
            ),
            open_lake=open_lake,
        )
    return InProcessLabExecutor()

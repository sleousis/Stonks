"""BackupService: a consistent backup taken inside the server process.

``stonks serve`` holds the lake, so a backup from another process can't
open it; this job kind backs up through the server's own lake connection
on the ``lake_write`` lane (never beside an ingest). The scheduler starts
it (``backup`` action) and a transport can expose it as a route
(``POST /api/backups`` returning the job, ``GET /api/backups/jobs/{id}/result``
returning :class:`BackupResultView`)."""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel

from stonks.app.context import AppContext
from stonks.app.jobs import Job, JobContext, JobRunner
from stonks.ops.backup import run_configured_backup

BACKUP_JOB = "backup"


class BackupResultView(BaseModel):
    backup_id: str
    #: Older backups the ``[backup.retention]`` policy removed.
    pruned: list[str]


class BackupService:
    def __init__(self, context: AppContext, runner: JobRunner) -> None:
        self._ctx = context
        self._runner = runner
        runner.register(BACKUP_JOB, self._handle, lock="lake_write")

    def submit(self, *, owner_id: str | None = None) -> Job:
        return self._runner.submit(BACKUP_JOB, {}, owner_id=owner_id)

    def run(self) -> BackupResultView:
        with self._ctx.lake() as lake:
            result = run_configured_backup(self._ctx.settings, lake=lake)
        return BackupResultView(backup_id=result.ref.id, pruned=result.pruned)

    def result(self, job_id: str) -> BackupResultView:
        """The result of a succeeded backup job (see ``JobService.typed_result``)."""
        job = self._runner.store.get(job_id)
        return BackupResultView.model_validate(job.result)

    def _handle(self, params: dict[str, Any], ctx: JobContext) -> BackupResultView:
        return self.run()

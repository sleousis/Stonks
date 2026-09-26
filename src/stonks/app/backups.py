"""BackupService: backups taken inside the server process, and the backups
on disk.

``stonks serve`` holds the lake, so a backup from another process can't
open it; this job kind backs up through the server's own lake connection
on the ``lake_write`` lane (never beside an ingest). The scheduler starts
it (``backup`` action); ``POST /api/backups`` returns the job and
``GET /api/backups/jobs/{id}/result`` its :class:`BackupResultView`.

The backups on disk can be listed and verified. A restore through the API
is staged: it verifies the backup and restores it into a new data folder
beside the backups, with migrations applied, and never touches the live
data. Switching to it means stopping every Stonks process first (see
``docs/operations.md``), which a running server can't do to itself.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from stonks.app.context import AppContext
from stonks.app.errors import NotFoundError, ValidationError
from stonks.app.jobs import Job, JobContext, JobRunner
from stonks.ops.backup import (
    BackupError,
    DataPaths,
    configured_target,
    read_manifest,
    run_configured_backup,
    verify_backup,
)
from stonks.ops.restore import RestoreError, restore_backup

BACKUP_JOB = "backup"
RESTORE_JOB = "backup_restore"


def restore_phrase(backup_id: str) -> str:
    """What an admin types to confirm a restore."""
    return f"RESTORE {backup_id}"


class BackupResultView(BaseModel):
    backup_id: str
    #: Older backups the ``[backup.retention]`` policy removed.
    pruned: list[str]


class BackupView(BaseModel):
    id: str
    created_at: datetime
    #: Sum of the files the manifest lists.
    size_bytes: int


class VerifyView(BaseModel):
    backup_id: str
    ok: bool
    problems: list[str]


class RestoreRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    #: Type ``RESTORE <backup id>`` exactly.
    confirmation: str = Field(max_length=100)


class RestoreResultView(BaseModel):
    backup_id: str
    #: The new data folder (lake, state, artifacts) the backup was restored into.
    data_dir: str
    lake_migrations_applied: list[int]
    state_migrations_applied: list[int]
    #: How to switch the install to the restored data.
    next_steps: list[str]


class BackupService:
    def __init__(self, context: AppContext, runner: JobRunner) -> None:
        self._ctx = context
        self._runner = runner
        runner.register(BACKUP_JOB, self._handle, lock="lake_write")
        runner.register(RESTORE_JOB, self._handle_restore, lock="lake_write")

    def submit(self, *, owner_id: str | None = None) -> Job:
        return self._runner.submit(BACKUP_JOB, {}, owner_id=owner_id)

    def run(self) -> BackupResultView:
        with self._ctx.lake() as lake:
            result = run_configured_backup(self._ctx.settings, lake=lake)
        return BackupResultView(backup_id=result.ref.id, pruned=result.pruned)

    # ---- backups on disk -------------------------------------------------------------

    def list(self) -> list[BackupView]:
        """Backups under the configured target, newest first."""
        target = configured_target(self._ctx.settings)
        out = []
        for ref in target.list():
            try:
                files = read_manifest(target.fetch(ref.id)).get("files", [])
            except BackupError:
                continue
            size = sum(int(f.get("size", 0)) for f in files)
            out.append(BackupView(id=ref.id, created_at=ref.created_at, size_bytes=size))
        return out

    def verify(self, backup_id: str) -> VerifyView:
        """Re-check the backup's checksums, schema versions and row counts."""
        report = verify_backup(self._path(backup_id))
        return VerifyView(backup_id=backup_id, ok=report.ok, problems=list(report.problems))

    def submit_restore(
        self, backup_id: str, request: RestoreRequest, *, owner_id: str | None = None
    ) -> Job:
        """Queue a staged restore. ``request.confirmation`` must be
        :func:`restore_phrase` exactly."""
        if request.confirmation.strip() != restore_phrase(backup_id):
            raise ValidationError(f"type {restore_phrase(backup_id)!r} to restore this backup")
        self._path(backup_id)
        return self._runner.submit(RESTORE_JOB, {"backup_id": backup_id}, owner_id=owner_id)

    def restore(self, backup_id: str) -> RestoreResultView:
        source = self._path(backup_id)
        stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
        data_dir = configured_target(self._ctx.settings).root / "restores" / f"{backup_id}-{stamp}"
        try:
            result = restore_backup(source, DataPaths.under(data_dir))
        except RestoreError as exc:
            raise ValidationError(str(exc)) from None
        return RestoreResultView(
            backup_id=backup_id,
            data_dir=str(data_dir),
            lake_migrations_applied=result.lake_migrations_applied,
            state_migrations_applied=result.state_migrations_applied,
            next_steps=[
                "Stop every Stonks process (server, scheduler, CLI).",
                f"Point STONKS_DATA_DIR at {data_dir}, or move its files over the live data.",
                "Start Stonks again.",
            ],
        )

    # ---- helpers ---------------------------------------------------------------------

    def _path(self, backup_id: str) -> Path:
        try:
            return configured_target(self._ctx.settings).fetch(backup_id)
        except BackupError:
            raise NotFoundError(f"no backup {backup_id!r}") from None

    def _handle(self, params: dict[str, Any], ctx: JobContext) -> BackupResultView:
        return self.run()

    def _handle_restore(self, params: dict[str, Any], ctx: JobContext) -> RestoreResultView:
        return self.restore(str(params["backup_id"]))

"""Backups taken inside the server process, which holds the lake (the
scheduler's ``backup`` job calls these through the api backend). Admins
only."""

from __future__ import annotations

from fastapi import APIRouter, Depends, Response

from stonks.api.deps import OptionalPrincipalDep, PrincipalDep, ServicesDep, require_permission
from stonks.api.errors import PROBLEM_RESPONSES
from stonks.api.routers._jobs_common import JOB_CREATED, accepted
from stonks.app.backups import BACKUP_JOB, BackupResultView
from stonks.app.jobs import Job
from stonks.auth import Permission

router = APIRouter(prefix="/api/backups", tags=["backups"], responses=PROBLEM_RESPONSES)

#: A backup is an operation (``operations.run``: admins with the admin scope).
ADMIN_ONLY = [Depends(require_permission(Permission.OPERATIONS_RUN))]


@router.post("", **JOB_CREATED, operation_id="startBackup", dependencies=ADMIN_ONLY)
def start_backup(services: ServicesDep, principal: PrincipalDep, response: Response) -> Job:
    """Queue a backup of the lake, state DB and artifacts to ``[backup]``'s
    target, pruned by its retention. Runs beside no ingest (lake lock)."""
    return accepted(services.backups.submit(owner_id=principal.user_id), response)


@router.get(
    "/jobs/{job_id}/result",
    response_model=BackupResultView,
    operation_id="getBackupResult",
    dependencies=ADMIN_ONLY,
)
def get_backup_result(
    job_id: str, services: ServicesDep, principal: OptionalPrincipalDep
) -> BackupResultView:
    """The result of a succeeded backup job (409 until it has succeeded)."""
    return services.jobs.typed_result(job_id, BACKUP_JOB, BackupResultView, principal)

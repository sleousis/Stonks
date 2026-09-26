"""Backups taken inside the server process, which holds the lake (the
scheduler's ``backup`` job calls these through the api backend), and the
backups on disk: list, verify and a staged restore. Admins only; a
restore also needs a fresh second factor and a typed confirmation."""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, Path, Response

from stonks.api.deps import (
    OptionalPrincipalDep,
    PageDep,
    PrincipalDep,
    ServicesDep,
    require_permission,
)
from stonks.api.errors import PROBLEM_RESPONSES
from stonks.api.routers._jobs_common import JOB_CREATED, accepted
from stonks.app.backups import (
    BACKUP_JOB,
    RESTORE_JOB,
    BackupResultView,
    BackupView,
    RestoreRequest,
    RestoreResultView,
    VerifyView,
)
from stonks.app.jobs import Job
from stonks.app.pagination import Page, page_of
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


BackupId = Annotated[str, Path(max_length=64)]


@router.get(
    "", response_model=Page[BackupView], operation_id="listBackups", dependencies=ADMIN_ONLY
)
def list_backups(services: ServicesDep, page: PageDep) -> Page[BackupView]:
    """Backups under ``[backup].dir``, newest first."""
    return page_of(services.backups.list(), page)


@router.post(
    "/{backup_id}/verify",
    response_model=VerifyView,
    operation_id="verifyBackup",
    dependencies=ADMIN_ONLY,
)
def verify_backup(backup_id: BackupId, services: ServicesDep) -> VerifyView:
    """Re-check a backup's checksums, schema versions and row counts. Takes a
    while for a large lake."""
    return services.backups.verify(backup_id)


@router.post(
    "/{backup_id}/restore",
    **JOB_CREATED,
    operation_id="restoreBackup",
    dependencies=[Depends(require_permission(Permission.BACKUP_RESTORE))],
)
def restore_backup(
    backup_id: BackupId,
    body: RestoreRequest,
    services: ServicesDep,
    principal: PrincipalDep,
    response: Response,
) -> Job:
    """Queue a staged restore: the backup is verified and restored into a new
    data folder beside the backups, with migrations applied. The live data
    is not touched; the result says how to switch. Needs a second factor
    from the last few minutes and ``confirmation`` = ``RESTORE <backup id>``."""
    return accepted(
        services.backups.submit_restore(backup_id, body, owner_id=principal.user_id), response
    )


@router.get(
    "/restores/{job_id}/result",
    response_model=RestoreResultView,
    operation_id="getRestoreResult",
    dependencies=ADMIN_ONLY,
)
def get_restore_result(
    job_id: str, services: ServicesDep, principal: OptionalPrincipalDep
) -> RestoreResultView:
    """Where a succeeded restore job put the data (409 until it has succeeded)."""
    return services.jobs.typed_result(job_id, RESTORE_JOB, RestoreResultView, principal)

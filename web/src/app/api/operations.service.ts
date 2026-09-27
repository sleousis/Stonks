import { Injectable } from '@angular/core';

import { unwrap } from './api-call';
import {
  getBackupResult,
  getRestoreResult,
  listBackups,
  listStatementFlags,
  restoreBackup,
  startBackup,
  verifyBackup,
} from './generated/sdk.gen';
import type { ListStatementFlagsData } from './models';

/** How many backups the Schedule page lists (newest first). */
export const BACKUPS_SHOWN = 50;

/** The text `POST /api/backups/{id}/restore` needs typed. */
export function restoreConfirmation(backupId: string): string {
  return `RESTORE ${backupId}`;
}

/** Backups on the server (take, list, verify, staged restore) and the statement audit's flags. */
@Injectable({ providedIn: 'root' })
export class OperationsService {
  /** Starts a backup job (verify and prune included); follow it, then read `backupResult`. */
  startBackup() {
    return unwrap(startBackup());
  }

  backupResult(jobId: string) {
    return unwrap(getBackupResult({ path: { job_id: jobId } }));
  }

  /**
   * Every backup folder on disk, newest first, with its size. Includes the
   * ones the nightly job or an operator made outside the console.
   */
  backups(limit = BACKUPS_SHOWN) {
    return unwrap(listBackups({ query: { limit } }));
  }

  /** Checks a backup's files against its manifest. */
  verifyBackup(backupId: string) {
    return unwrap(verifyBackup({ path: { backup_id: backupId } }));
  }

  /**
   * Starts a staged restore into a new folder (the live data is never
   * touched). Needs a fresh second factor. Returns the job, then read `restoreResult`.
   */
  restoreBackup(backupId: string) {
    return unwrap(
      restoreBackup({
        path: { backup_id: backupId },
        body: { confirmation: restoreConfirmation(backupId) },
      }),
    );
  }

  /** Where a staged restore put the data and how to switch to it. */
  restoreResult(jobId: string) {
    return unwrap(getRestoreResult({ path: { job_id: jobId } }));
  }

  statementFlags(query?: ListStatementFlagsData['query']) {
    return unwrap(listStatementFlags({ query }));
  }
}

import { Injectable } from '@angular/core';

import { unwrap } from './api-call';
import { getBackupResult, listJobs, listStatementFlags, startBackup } from './generated/sdk.gen';
import type { ListStatementFlagsData } from './models';

/** The job kind of a server-side backup. */
export const BACKUP_JOB_KIND = 'backup';

/** Backups taken by the server, and the statement audit's flags. */
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
   * Backup jobs the server ran, newest first. The API has no route listing the
   * backup folders on disk, so this is the history the console can show.
   */
  backupJobs(limit = 20) {
    return unwrap(listJobs({ query: { kind: BACKUP_JOB_KIND, limit } }));
  }

  statementFlags(query?: ListStatementFlagsData['query']) {
    return unwrap(listStatementFlags({ query }));
  }
}

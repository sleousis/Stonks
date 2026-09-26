import { Injectable } from '@angular/core';

import { SILENT_HEADERS } from '../core/http/interceptors';
import { unwrap } from './api-call';
import { cancelJob, createStreamToken, getJob, listJobs } from './generated/sdk.gen';
import type { ListJobsData } from './models';

/**
 * Raw job endpoints. Pages use JobsService (core/jobs), which adds live
 * progress over SSE; this class only maps the REST calls.
 */
@Injectable({ providedIn: 'root' })
export class JobsApiService {
  list(query?: ListJobsData['query']) {
    return unwrap(listJobs({ query }));
  }

  /** `silent` skips the error toast (used by background polling). */
  get(jobId: string, silent = false) {
    return unwrap(
      getJob({ path: { job_id: jobId }, headers: silent ? SILENT_HEADERS : undefined }),
    );
  }

  cancel(jobId: string) {
    return unwrap(cancelJob({ path: { job_id: jobId } }));
  }

  /** Short-lived token for the job's event stream. Failures fall back to polling, so no toast. */
  streamToken(jobId: string) {
    return unwrap(createStreamToken({ path: { job_id: jobId }, headers: SILENT_HEADERS }));
  }
}

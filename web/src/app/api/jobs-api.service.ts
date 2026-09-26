import { Injectable } from '@angular/core';

import { SILENT_HEADERS } from '../core/http/interceptors';
import { unwrap } from './api-call';
import { cancelJob, createStreamToken, getJob, listJobs } from './generated/sdk.gen';
import type { ListJobsData } from './models';

/** A job id no job ever has (real ids are `job_<hex>`), used by `probeToken()`. */
export const TOKEN_PROBE_JOB_ID = 'token-check';

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

  /**
   * Checks the bearer token without changing anything: asks for a stream
   * token for a job id that never exists. The route always needs the bearer
   * token, so 404 means "token accepted", 401 "rejected", 503 "the API has no
   * token configured". Silent: Settings explains the outcome itself.
   */
  probeToken() {
    return unwrap(
      createStreamToken({ path: { job_id: TOKEN_PROBE_JOB_ID }, headers: SILENT_HEADERS }),
    );
  }

  /** Short-lived token for the job's event stream. Failures fall back to polling, so no toast. */
  streamToken(jobId: string) {
    return unwrap(createStreamToken({ path: { job_id: jobId }, headers: SILENT_HEADERS }));
  }
}

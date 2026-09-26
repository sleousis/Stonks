import { Injectable } from '@angular/core';

import { SILENT_HEADERS } from '../core/http/interceptors';
import { unwrap } from './api-call';
import { getSchedule, runScheduledJobNow } from './generated/sdk.gen';
import type { GetScheduleData, RunNowRequest } from './models';

/** The scheduler: jobs with their next run, recent runs, and run-now. */
@Injectable({ providedIn: 'root' })
export class ScheduleService {
  /** `silent` skips the error toast (background reads such as the session strip). */
  overview(query?: GetScheduleData['query'], silent = false) {
    return unwrap(getSchedule({ query, headers: silent ? SILENT_HEADERS : undefined }));
  }

  /** Starts in the background; the run shows up in `overview()`. */
  runNow(job: string, body: RunNowRequest = {}) {
    return unwrap(runScheduledJobNow({ path: { job }, body }));
  }
}

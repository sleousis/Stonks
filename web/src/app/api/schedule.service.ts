import { Injectable } from '@angular/core';

import { unwrap } from './api-call';
import { getSchedule, runScheduledJobNow } from './generated/sdk.gen';
import type { GetScheduleData, RunNowRequest } from './models';

/** The scheduler: jobs with their next run, recent runs, and run-now. */
@Injectable({ providedIn: 'root' })
export class ScheduleService {
  overview(query?: GetScheduleData['query']) {
    return unwrap(getSchedule({ query }));
  }

  /** Starts in the background; the run shows up in `overview()`. */
  runNow(job: string, body: RunNowRequest = {}) {
    return unwrap(runScheduledJobNow({ path: { job }, body }));
  }
}

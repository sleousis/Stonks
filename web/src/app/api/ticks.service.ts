import { Injectable } from '@angular/core';

import { unwrap } from './api-call';
import { getTick, getTickResult, listTicks, startTick } from './generated/sdk.gen';
import type { ListTicksData, TickRequest } from './models';

/** Production ticks: history, detail, and starting one (a background job). */
@Injectable({ providedIn: 'root' })
export class TicksService {
  list(query?: ListTicksData['query']) {
    return unwrap(listTicks({ query }));
  }

  get(tickId: string) {
    return unwrap(getTick({ path: { tick_id: tickId } }));
  }

  /** Queues a tick and returns its Job; follow it with JobsService.track(). */
  start(body: TickRequest) {
    return unwrap(startTick({ body }));
  }

  result(jobId: string) {
    return unwrap(getTickResult({ path: { job_id: jobId } }));
  }
}

import { Injectable, signal } from '@angular/core';

import { unwrap } from './api-call';
import { getTick, getTickResult, listTicks, startTick } from './generated/sdk.gen';
import type { ListTicksData, TickRequest } from './models';

/** Production ticks: history, detail, and starting one (a background job). */
@Injectable({ providedIn: 'root' })
export class TicksService {
  private readonly finishedCount = signal(0);
  /**
   * Bumps each time a tick job this tab followed ends. Pages that show
   * tick results (the dashboard) reload when it changes.
   */
  readonly finished = this.finishedCount.asReadonly();

  /** Call when a followed tick job ends, whatever its outcome. */
  announceFinished(): void {
    this.finishedCount.update((n) => n + 1);
  }

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

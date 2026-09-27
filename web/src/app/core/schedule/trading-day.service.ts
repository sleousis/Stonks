import { Injectable, inject, signal } from '@angular/core';

import type { MarketSessionsView, ScheduledJobView } from '../../api/models';
import { ScheduleService } from '../../api/schedule.service';

/**
 * The trading day as the app last read it from `GET /api/schedule`: the
 * scheduled jobs (with their next run) and the market sessions. The
 * session strip loads and polls it; Today reads the next run from here, so
 * the schedule is asked for once.
 */
@Injectable({ providedIn: 'root' })
export class TradingDayService {
  private readonly schedule = inject(ScheduleService);

  private readonly jobsSignal = signal<readonly ScheduledJobView[]>([]);
  private readonly marketSignal = signal<MarketSessionsView | null>(null);

  readonly jobs = this.jobsSignal.asReadonly();
  readonly market = this.marketSignal.asReadonly();

  /** Read quietly (no error toast). Keeps the last read when it fails. */
  async load(): Promise<void> {
    try {
      const view = await this.schedule.overview({ limit: 1 }, true);
      this.jobsSignal.set(view.jobs);
      this.marketSignal.set(view.market ?? null);
    } catch {
      // No scheduler or signed out: nothing to show.
    }
  }
}

import { DestroyRef, Injectable, computed, inject, signal } from '@angular/core';

import type { MarketSessionsView, ScheduledJobView } from '../../api/models';
import { ScheduleService } from '../../api/schedule.service';
import { nextTradingRun } from './job-labels';

/** How long after a trading run's start `runsPassed` ticks (the run needs a moment). */
export const RUN_PASSED_GRACE_MS = 5_000;
/** setTimeout's ceiling; a run further out is armed again on a later read. */
const MAX_TIMER_MS = 2 ** 31 - 1;

/**
 * The trading day as the app last read it from `GET /api/schedule`: the
 * scheduled jobs (with their next run) and the market sessions. The
 * session strip loads and polls it; Today reads the next run from here, so
 * the schedule is asked for once.
 *
 * `runsPassed` counts the trading runs whose start time went by while the
 * app was open. A page puts it in its resource params (or `autoRefresh`
 * triggers) to reload once a run has happened:
 *
 *   autoRefresh(() => [this.ticks], { triggers: [inject(TradingDayService).runsPassed] });
 */
@Injectable({ providedIn: 'root' })
export class TradingDayService {
  private readonly schedule = inject(ScheduleService);

  private readonly jobsSignal = signal<readonly ScheduledJobView[]>([]);
  private readonly marketSignal = signal<MarketSessionsView | null>(null);
  private readonly loadedSignal = signal(false);
  private readonly passed = signal(0);
  private timer: ReturnType<typeof setTimeout> | null = null;

  readonly jobs = this.jobsSignal.asReadonly();
  readonly market = this.marketSignal.asReadonly();
  /** True once a read succeeded: an empty `jobs` then means nothing is scheduled. */
  readonly loaded = this.loadedSignal.asReadonly();
  /** Goes up by one each time a scheduled trading run's start time passes. */
  readonly runsPassed = this.passed.asReadonly();
  /** A trading run is scheduled at all (any time ahead). */
  readonly hasTradingRun = computed(() => this.jobsSignal().some((j) => j.action === 'tick'));

  constructor() {
    inject(DestroyRef).onDestroy(() => this.disarm());
  }

  /** Read quietly (no error toast). Keeps the last read when it fails. */
  async load(): Promise<void> {
    try {
      const view = await this.schedule.overview({ limit: 1 }, true);
      this.jobsSignal.set(view.jobs);
      this.marketSignal.set(view.market ?? null);
      this.loadedSignal.set(true);
      this.arm();
    } catch {
      // No scheduler or signed out: nothing to show.
    }
  }

  /** Wake up just after the next trading run starts, count it and read again. */
  private arm(): void {
    this.disarm();
    const now = Date.now();
    const run = nextTradingRun(this.jobsSignal(), now);
    const at = run?.next_run_at ? Date.parse(run.next_run_at) : NaN;
    // A run already under way was counted when its time came (or the app opened after it).
    if (!Number.isFinite(at) || at <= now) return;
    const delay = at - now + RUN_PASSED_GRACE_MS;
    if (delay > MAX_TIMER_MS) return;
    this.timer = setTimeout(() => {
      this.timer = null;
      this.passed.update((n) => n + 1);
      void this.load();
    }, delay);
  }

  private disarm(): void {
    if (this.timer) clearTimeout(this.timer);
    this.timer = null;
  }
}

import { DestroyRef, Injectable, type Signal, inject, signal } from '@angular/core';

import { nextTradingRun } from '../../core/schedule/job-labels';
import { TradingDayService } from '../../core/schedule/trading-day.service';

/** How often the clock looks at the schedule. */
export const RUN_CHECK_MS = 5_000;

/**
 * Counts scheduled trading runs whose start time has passed while Today is
 * open (UX-12). Today's cards list `count` as an `autoRefresh` trigger, so the
 * blotter, the tape and the value reload when a run starts, even one this tab
 * did not start.
 *
 * Stand-in until `TradingDayService` has its own `runsPassed` signal: swap
 * `inject(RunsPassed).count` for that signal and delete this file.
 */
@Injectable({ providedIn: 'root' })
export class RunsPassed {
  private readonly day = inject(TradingDayService);
  private readonly passed = signal(0);
  /** The start time (epoch ms) of the run we are waiting for, if any. */
  private waitingFor: number | null = null;

  /** Goes up by one each time a scheduled trading run's start time passes. */
  readonly count: Signal<number> = this.passed.asReadonly();

  constructor() {
    this.check(Date.now());
    const timer = setInterval(() => this.check(Date.now()), RUN_CHECK_MS);
    inject(DestroyRef).onDestroy(() => clearInterval(timer));
  }

  /** @internal Exposed for tests: look at the schedule as of `now`. */
  check(now: number): void {
    if (this.waitingFor !== null && now >= this.waitingFor) {
      this.waitingFor = null;
      this.passed.update((n) => n + 1);
    }
    const job = nextTradingRun(this.day.jobs(), now);
    const at = job?.next_run_at ? Date.parse(job.next_run_at) : NaN;
    // Only runs still ahead: a run that just passed stays listed for a minute.
    if (Number.isFinite(at) && at > now) this.waitingFor = at;
  }
}

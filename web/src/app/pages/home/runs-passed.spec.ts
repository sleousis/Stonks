import { signal } from '@angular/core';
import { TestBed } from '@angular/core/testing';

import type { ScheduledJobView } from '../../api/models';
import { TradingDayService } from '../../core/schedule/trading-day.service';
import { RunsPassed } from './runs-passed';

const job = (action: string, at: number): ScheduledJobView => ({
  action,
  name: action,
  next_run_at: new Date(at).toISOString(),
  next_as_of: null,
  trigger: 'daily',
});

describe('RunsPassed', () => {
  const NOW = Date.parse('2026-09-28T20:40:00Z');
  const jobs = signal<ScheduledJobView[]>([]);
  let runs: RunsPassed;

  beforeEach(() => {
    jobs.set([job('connections_sync', NOW + 60_000), job('tick', NOW + 5 * 60_000)]);
    TestBed.configureTestingModule({
      providers: [{ provide: TradingDayService, useValue: { jobs } }],
    });
    runs = TestBed.inject(RunsPassed);
  });

  it('counts once when the trading run starts, and ignores system jobs', () => {
    runs.check(NOW);
    runs.check(NOW + 2 * 60_000); // the broker sync passed
    expect(runs.count()).toBe(0);
    runs.check(NOW + 5 * 60_000 + 1_000);
    expect(runs.count()).toBe(1);
    // The run stays listed for a minute until the schedule is read again.
    runs.check(NOW + 5 * 60_000 + 30_000);
    expect(runs.count()).toBe(1);
  });

  it('waits for the next run once the schedule moves on', () => {
    runs.check(NOW);
    runs.check(NOW + 6 * 60_000);
    jobs.set([job('tick', NOW + 24 * 3_600_000)]);
    runs.check(NOW + 7 * 60_000);
    runs.check(NOW + 24 * 3_600_000 + 1);
    expect(runs.count()).toBe(2);
  });
});

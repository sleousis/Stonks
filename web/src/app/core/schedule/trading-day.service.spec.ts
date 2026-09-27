import { TestBed } from '@angular/core/testing';

import type { ScheduleView, ScheduledJobView } from '../../api/models';
import { ScheduleService } from '../../api/schedule.service';
import { RUN_PASSED_GRACE_MS, TradingDayService } from './trading-day.service';

const NOW = Date.parse('2026-09-28T14:00:00Z');

function job(action: string, at: string | null): ScheduledJobView {
  return { name: action, action, trigger: 'x', next_run_at: at, next_as_of: null };
}

function view(jobs: ScheduledJobView[]): ScheduleView {
  return { backend: 'in_process', hosted: true, recent: [], jobs };
}

describe('TradingDayService', () => {
  let overview: ReturnType<typeof vi.fn>;

  function create() {
    TestBed.configureTestingModule({
      providers: [{ provide: ScheduleService, useValue: { overview } }],
    });
    return TestBed.inject(TradingDayService);
  }

  beforeEach(() => {
    vi.useFakeTimers({ now: NOW });
    overview = vi.fn();
  });
  afterEach(() => {
    TestBed.resetTestingModule();
    vi.useRealTimers();
  });

  it('reads the schedule quietly and keeps the last read when a read fails', async () => {
    overview.mockResolvedValueOnce(view([job('tick', '2026-09-28T16:45:00Z')]));
    const day = create();
    expect(day.loaded()).toBe(false);
    await day.load();
    expect(overview).toHaveBeenCalledWith({ limit: 1 }, true);
    expect(day.loaded()).toBe(true);
    expect(day.hasTradingRun()).toBe(true);

    overview.mockRejectedValueOnce(new Error('down'));
    await day.load();
    expect(day.jobs().length).toBe(1);
  });

  it('is settled after the first read even when it fails', async () => {
    overview.mockRejectedValueOnce(new Error('no scheduler'));
    const day = create();
    expect(day.settled()).toBe(false);
    await day.load();
    expect(day.settled()).toBe(true);
    expect(day.loaded()).toBe(false);
  });

  it('counts a trading run once its time passes, then reads the schedule again', async () => {
    overview
      .mockResolvedValueOnce(
        view([
          job('connections_sync', '2026-09-28T14:05:00Z'),
          job('tick', '2026-09-28T14:10:00Z'),
        ]),
      )
      .mockResolvedValue(view([job('tick', '2026-09-29T14:10:00Z')]));
    const day = create();
    await day.load();
    expect(day.runsPassed()).toBe(0);

    // A system job passing does not count.
    await vi.advanceTimersByTimeAsync(5 * 60_000 + RUN_PASSED_GRACE_MS);
    expect(day.runsPassed()).toBe(0);

    await vi.advanceTimersByTimeAsync(5 * 60_000);
    expect(day.runsPassed()).toBe(1);
    expect(overview).toHaveBeenCalledTimes(2);
    expect(day.jobs()[0].next_run_at).toBe('2026-09-29T14:10:00Z');

    // Nothing more until tomorrow's run.
    await vi.advanceTimersByTimeAsync(60 * 60_000);
    expect(day.runsPassed()).toBe(1);
  });

  it('arms nothing when no trading run is scheduled', async () => {
    overview.mockResolvedValue(view([job('health', '2026-09-28T14:01:00Z')]));
    const day = create();
    await day.load();
    expect(day.hasTradingRun()).toBe(false);
    await vi.advanceTimersByTimeAsync(10 * 60_000);
    expect(day.runsPassed()).toBe(0);
    expect(overview).toHaveBeenCalledTimes(1);
  });
});

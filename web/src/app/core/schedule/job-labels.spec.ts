import type { ScheduledJobView } from '../../api/models';
import { jobLabel, nextJob, nextTradingRun } from './job-labels';

const job = (action: string, at: string | null, name = action): ScheduledJobView => ({
  action,
  name,
  next_run_at: at,
  next_as_of: null,
  trigger: 'daily',
});

describe('job labels', () => {
  it('calls the tick action a trading run', () => {
    expect(jobLabel(job('tick', null))).toBe('Trading run');
    expect(jobLabel(job('connections_sync', null))).toBe('Broker sync');
  });

  it('names every default job in words, never by its id', () => {
    expect(jobLabel(job('live_reconcile', null, 'live_sod_check'))).toBe(
      'Broker check before the open',
    );
    expect(jobLabel(job('live_reconcile', null, 'live_eod_check'))).toBe(
      'Broker check after the close',
    );
    expect(jobLabel(job('options_live', null, 'options_expiry_watch'))).toBe(
      'Options expiry watch',
    );
    expect(jobLabel(job('broker_health', null))).toBe('Broker gateway check');
    expect(jobLabel(job('engine_start', null))).toBe('Intraday engine start');
    expect(jobLabel(job('ingest_borrow', null))).toBe('Borrow rates update');
  });

  it('humanizes an unknown action from its name', () => {
    expect(jobLabel(job('custom_thing', null, 'nightly_export'))).toBe('Nightly export');
  });

  it('picks the next trading run over an earlier system job', () => {
    const now = Date.parse('2026-09-28T13:00:00Z');
    const jobs = [
      job('connections_sync', '2026-09-28T14:00:00Z'),
      job('tick', '2026-09-28T16:45:00Z'),
    ];
    expect(nextJob(jobs, now)?.action).toBe('connections_sync');
    expect(nextTradingRun(jobs, now)?.action).toBe('tick');
  });

  it('returns null without a scheduled trading run', () => {
    const now = Date.parse('2026-09-28T13:00:00Z');
    expect(nextTradingRun([job('health', '2026-09-28T14:00:00Z')], now)).toBeNull();
  });
});

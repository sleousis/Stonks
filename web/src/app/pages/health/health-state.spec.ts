import type { HealthCheckView } from '../../api/models';
import { checkLevel, checkThreshold, overallLevel, splitChecks, worstLevel } from './health-state';

function check(name: string, ok: boolean, detail = 'none'): HealthCheckView {
  return { name, ok, detail };
}

describe('checkLevel', () => {
  it('is good when the check passes', () => {
    expect(checkLevel(check('stuck_ticks', true))).toBe('good');
    expect(
      checkLevel(check('freshness:AAPL.US', true, 'latest bar 2026-09-25 (1d old, max 4d)')),
    ).toBe('good');
  });

  it('is a warning for data a little past its limit', () => {
    expect(
      checkLevel(check('freshness:AAPL.US', false, 'latest bar 2026-09-19 (7d old, max 4d)')),
    ).toBe('warning');
  });

  it('is critical for data more than twice its limit', () => {
    expect(
      checkLevel(check('freshness:AAPL.US', false, 'latest bar 2026-09-01 (25d old, max 4d)')),
    ).toBe('critical');
  });

  it('is critical when a ticker has no bars at all', () => {
    expect(checkLevel(check('freshness:NEW.US', false, 'no daily bars'))).toBe('critical');
  });

  it('is critical for stuck ticks and stuck ingest runs', () => {
    expect(checkLevel(check('stuck_ticks', false, 'running > 30m: t1'))).toBe('critical');
    expect(checkLevel(check('stuck_ingest_runs', false, 'running > 60m: #3 (prices)'))).toBe(
      'critical',
    );
  });

  it('is a warning for recent ingest failures', () => {
    expect(checkLevel(check('ingest_failures', false, 'failed in last 24h: #4 (prices)'))).toBe(
      'warning',
    );
  });

  it('is critical when the check itself crashed', () => {
    expect(checkLevel(check('freshness', false, 'check error: no such table'))).toBe('critical');
    expect(checkLevel(check('ingest_failures', false, 'check error: locked'))).toBe('critical');
  });

  it('is critical for a failing check it does not know', () => {
    expect(checkLevel(check('something_new', false, 'bad'))).toBe('critical');
  });
});

describe('overallLevel', () => {
  it('is the worst level of all checks', () => {
    expect(overallLevel([check('a', true), check('b', true)])).toBe('good');
    expect(
      overallLevel([check('stuck_ticks', true), check('ingest_failures', false, 'failed')]),
    ).toBe('warning');
    expect(
      overallLevel([check('ingest_failures', false, 'failed'), check('stuck_ticks', false, 'x')]),
    ).toBe('critical');
  });

  it('is good with no checks', () => {
    expect(overallLevel([])).toBe('good');
  });

  it('orders levels good < warning < critical', () => {
    expect(worstLevel(['good', 'critical', 'warning'])).toBe('critical');
  });
});

describe('splitChecks', () => {
  it('separates per-ticker freshness from run checks', () => {
    const split = splitChecks([
      check('freshness:AAPL.US', true, 'latest bar 2026-09-25 (1d old, max 4d)'),
      check('stuck_ticks', true),
      check('freshness:MSFT.US', false, 'no daily bars'),
      check('freshness', false, 'check error: boom'),
    ]);
    expect(split.freshness.map((f) => f.ticker)).toEqual(['MSFT.US', 'AAPL.US']);
    expect(split.freshness[0].level).toBe('critical');
    expect(split.other.map((c) => c.name)).toEqual(['freshness', 'stuck_ticks']);
  });
});

describe('checkThreshold', () => {
  const t = {
    max_bar_age_days: 4,
    stuck_tick_minutes: 30,
    stuck_ingest_minutes: 120,
    ingest_failure_lookback_hours: 24,
  };

  it('names the limit each check compares against', () => {
    expect(checkThreshold('stuck_ticks', t)).toBe('Stuck when running over 30 min');
    expect(checkThreshold('stuck_ingest_runs', t)).toBe('Stuck when running over 120 min');
    expect(checkThreshold('ingest_failures', t)).toBe('Failures in the last 24 h');
    expect(checkThreshold('freshness:AAPL.US', t)).toBe('Latest bar at most 4 days old');
  });

  it('shows nothing when the API sent no threshold', () => {
    expect(checkThreshold('stuck_ticks', {})).toBeNull();
    expect(checkThreshold('stuck_ticks', null)).toBeNull();
    expect(checkThreshold('something_new', t)).toBeNull();
  });
});

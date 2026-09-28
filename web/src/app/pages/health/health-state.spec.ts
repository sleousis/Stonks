import type { HealthCheckView } from '../../api/models';
import {
  checkLevel,
  checkMeaning,
  checkOutcome,
  checkPill,
  checkThreshold,
  checkTitle,
  overallLevel,
  plainDetail,
  splitChecks,
  worstLevel,
} from './health-state';

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
    expect(split.freshness.map((f) => f.detail)).toEqual([
      'No daily prices stored yet.',
      'Latest price 2026-09-25, 1 day old.',
    ]);
    // Failing first, then by name.
    expect(split.other.map((c) => c.name)).toEqual(['freshness', 'stuck_ticks']);
  });

  it('leaves broker gateway checks to their own panel, but counts them overall', () => {
    const checks = [check('stuck_ticks', true), check('broker:live', false, 'live gateway down')];
    expect(splitChecks(checks).other.map((c) => c.name)).toEqual(['stuck_ticks']);
    expect(overallLevel(checks)).toBe('critical');
  });
});

describe('check names and meanings', () => {
  const serverChecks = [
    'freshness',
    'stuck_ticks',
    'stuck_ingest_runs',
    'ingest_failures',
    'var_violations',
    'lab_queue',
    'risk_halts',
  ];

  it('gives every server check a plain name and a one-line meaning', () => {
    for (const id of serverChecks) {
      expect(checkTitle(id)).not.toContain('_');
      expect(checkMeaning(id)).toBeTruthy();
    }
    expect(checkTitle('lab_queue')).toBe('Lab workers');
    expect(checkTitle('var_violations')).toBe('Risk estimate accuracy');
    expect(checkTitle('risk_halts')).toBe('Trading stops');
  });

  it('names per-ticker and gateway checks, and writes unknown ids in words', () => {
    expect(checkTitle('freshness:AAPL.US')).toBe('Freshness of AAPL.US');
    expect(checkMeaning('freshness:AAPL.US')).toBe(checkMeaning('freshness'));
    expect(checkTitle('broker:paper')).toBe('Broker gateway paper');
    expect(checkTitle('something_new')).toBe('Something new');
    expect(checkMeaning('something_new')).toBeNull();
  });
});

describe('checkPill', () => {
  it('says Passed, Failed or Not enough data yet', () => {
    expect(checkPill(check('stuck_ticks', true))).toEqual({ label: 'Passed', tone: 'positive' });
    expect(checkPill(check('stuck_ticks', false, 'running > 30m: t1'))).toEqual({
      label: 'Failed',
      tone: 'negative',
    });
    expect(checkPill(check('ingest_failures', false, 'failed in last 24h: #4 (prices)'))).toEqual({
      label: 'Failed',
      tone: 'warn',
    });
    expect(checkPill(check('var_violations', true, 'not enough days yet'))).toEqual({
      label: 'Not enough data yet',
      tone: 'neutral',
    });
    expect(checkOutcome(check('var_violations', true, 'within band'))).toBe('passed');
  });
});

describe('plainDetail', () => {
  it('turns the server details into plain words without ids', () => {
    const cases: [string, boolean, string, string][] = [
      ['stuck_ticks', true, 'none', 'None.'],
      ['stuck_ticks', false, 'running > 60m: t1, t2', '2 trading runs running over 60 minutes.'],
      [
        'stuck_ingest_runs',
        false,
        'running > 180m: #3 (prices)',
        '1 data update running over 180 minutes.',
      ],
      [
        'ingest_failures',
        false,
        'failed in last 24h: #4 (prices), #5 (metadata)',
        '2 data updates failed in the last 24 hours.',
      ],
      ['var_violations', true, 'not enough days yet', 'Too few trading days scored to judge yet.'],
      ['var_violations', true, 'within band', 'Within the expected range.'],
      [
        'var_violations',
        false,
        'outside 0.5-2: pf_a ratio 3.10 over 60d',
        '1 portfolio outside the expected range.',
      ],
      [
        'lab_queue',
        true,
        '0 queued, 0 running, 0 worker(s) alive',
        '0 waiting, 0 running, 0 workers up.',
      ],
      ['lab_queue', true, 'no lab queue table', 'Lab workers are not in use.'],
      [
        'lab_queue',
        false,
        'oldest job waited 45 min with no live worker; 1 queued, 0 running, 0 worker(s) alive',
        'A job waited 45 minutes with no worker running. 1 waiting, 0 running, 0 workers up.',
      ],
      ['risk_halts', true, 'no halt in force', 'No stop in force.'],
      [
        'risk_halts',
        false,
        '#3 kill_switch (global, all); #4 circuit_breaker (pf_a, buys)',
        '2 stops in force.',
      ],
      ['freshness', false, 'check error: no such table', 'The check could not run: no such table.'],
    ];
    for (const [name, ok, detail, words] of cases) {
      expect(plainDetail(check(name, ok, detail)), detail).toBe(words);
    }
  });

  it('keeps an unknown detail but starts it with a capital', () => {
    expect(plainDetail(check('something_new', false, 'bad thing'))).toBe('Bad thing');
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
    expect(checkThreshold('freshness:AAPL.US', t)).toBe('Latest price at most 4 days old');
  });

  it('shows nothing when the API sent no threshold', () => {
    expect(checkThreshold('stuck_ticks', {})).toBeNull();
    expect(checkThreshold('stuck_ticks', null)).toBeNull();
    expect(checkThreshold('something_new', t)).toBeNull();
  });
});

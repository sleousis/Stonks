import { CANDIDATE_V2, swapReport, versionEvent } from '../../testing/model-version-fixtures';
import {
  calibrationRow,
  compareBooks,
  eventWords,
  newestFirst,
  swapCheckRow,
  trainWindow,
  versionLook,
} from './model-versions';

describe('model version words', () => {
  it('names each version status in trader words', () => {
    expect(versionLook('live').label).toBe('Model in use');
    expect(versionLook('candidate')).toMatchObject({ label: 'Candidate', tone: 'info' });
    expect(versionLook('failed').tone).toBe('negative');
    expect(versionLook('something_new').label).toBe('something_new');
  });

  it('says what each log entry means', () => {
    expect(eventWords('swap')).toBe('Swapped in');
    expect(eventWords('supersede')).toBe('Replaced by a newer candidate');
    expect(eventWords('later_kind')).toBe('later_kind');
  });

  it('writes the training window, or a dash without one', () => {
    expect(trainWindow(CANDIDATE_V2)).toBe('2024-09-01 to 2026-09-19');
    expect(trainWindow({ train_start: null, train_end: null })).toBe('–');
  });
});

describe('swapCheckRow', () => {
  const [candidate, days, drawdown, vsLive] = swapReport(false).checks;

  it('formats days as a count against a floor', () => {
    expect(swapCheckRow(days)).toMatchObject({
      label: 'Test book days',
      passed: false,
      value: '3',
      limit: '≥ 20',
    });
  });

  it('formats the drawdown as a percentage and the paired test as a t-statistic', () => {
    expect(swapCheckRow(drawdown)).toMatchObject({ value: '4.00%', limit: '≤ 25.00%' });
    const row = swapCheckRow(vsLive);
    expect(row.label).toBe('Against the model in use');
    expect(row.value).toContain('2.4');
    expect(row.limit).toContain('1.6');
  });

  it('shows dashes for a check without figures', () => {
    expect(swapCheckRow(candidate)).toMatchObject({ value: '–', limit: '–' });
  });
});

describe('compareBooks', () => {
  it('sizes both bars against the larger return and works out the gap', () => {
    const b = compareBooks(swapReport(true));
    expect(b.candidateWidth).toBe(100);
    expect(b.liveWidth).toBe(58);
    expect(b.gap).toBeCloseTo(0.013);
  });

  it('has no gap while a book has no days', () => {
    const b = compareBooks(swapReport(false, { live_return: null, candidate_return: null }));
    expect(b.gap).toBeNull();
    expect(b.candidateWidth).toBe(0);
  });
});

describe('newestFirst', () => {
  it('orders the log newest first', () => {
    const log = newestFirst([
      versionEvent({ id: 1 }),
      versionEvent({ id: 2, created_at: '2026-09-20T06:00:00Z' }),
    ]);
    expect(log.map((e) => e.id)).toEqual([2, 1]);
  });
});

describe('calibrationRow', () => {
  const base = {
    strategy_id: 'm',
    version: 3,
    n_forecasts: 22,
    n_resolved: 20,
    brier: 0.18,
    brier_base_rate: 0.25,
    skill: 0.28,
    base_rate: 0.5,
    mean_forecast: 0.6,
    ece: 0.05,
    bins: [],
  };

  it('formats a resolved model and says whether it beats the base rate', () => {
    const row = calibrationRow(base);
    expect(row).toMatchObject({ version: 3, resolved: '20 of 22', beatsBaseRate: true });
    expect(row.brier).toContain('0.18');
    expect(calibrationRow({ ...base, skill: -0.1 }).beatsBaseRate).toBe(false);
  });

  it('waits for resolved forecasts before judging', () => {
    const row = calibrationRow({ ...base, n_resolved: 0, brier: null, skill: null, ece: null });
    expect(row.beatsBaseRate).toBeNull();
    expect(row.brier).toBe('–');
  });
});

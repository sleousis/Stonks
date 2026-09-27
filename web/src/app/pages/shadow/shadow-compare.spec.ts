import type { PnlRowView } from '../../api/models';
import { compareToReal, comparisonSeries, normalizeTo100, returnOf } from './shadow-compare';

function row(day: string, total_value: number, drawdown = 0, days_elapsed?: number): PnlRowView {
  return {
    day,
    total_value,
    drawdown,
    daily_change: null,
    daily_return: null,
    cumulative_return: null,
    days_elapsed,
  };
}

// Real portfolio: $200k, +10% by day 3, then flat.
const REAL = [
  row('2026-09-01', 200_000),
  row('2026-09-02', 210_000),
  row('2026-09-03', 220_000),
  row('2026-09-04', 220_000),
];
// Shadow: starts a day later with $100k, +5% by day 4.
const SHADOW = [
  row('2026-09-02', 100_000, 0, 0),
  row('2026-09-03', 98_000, -0.02, 1),
  row('2026-09-04', 105_000, 0, 2),
];

describe('normalizeTo100', () => {
  it('rebases to 100 on the first day', () => {
    expect(normalizeTo100(REAL)).toEqual([
      { time: '2026-09-01', value: 100 },
      { time: '2026-09-02', value: 105 },
      { time: '2026-09-03', value: 110 },
      { time: '2026-09-04', value: 110 },
    ]);
  });

  it('rebases on a later day and drops earlier rows', () => {
    const points = normalizeTo100(REAL, '2026-09-02');
    expect(points[0]).toEqual({ time: '2026-09-02', value: 100 });
    expect(points.at(-1)?.value).toBeCloseTo((220 / 210) * 100);
  });

  it('uses the first day after the base when the base day is missing, and sorts', () => {
    const shuffled = [REAL[3], REAL[2], REAL[0]];
    expect(normalizeTo100(shuffled, '2026-09-02')[0]).toEqual({ time: '2026-09-03', value: 100 });
  });

  it('returns nothing for empty or zero-value series', () => {
    expect(normalizeTo100([])).toEqual([]);
    expect(normalizeTo100([row('2026-09-01', 0)])).toEqual([]);
  });

  it('reads returns off normalized points', () => {
    const points = normalizeTo100(REAL);
    expect(returnOf(points)).toBeCloseTo(0.1);
    expect(returnOf(points, '2026-09-02')).toBeCloseTo(0.05);
    expect(returnOf([])).toBeNull();
  });
});

describe('compareToReal', () => {
  it('compares shadow and real over the shadow window, not the real history', () => {
    const c = compareToReal('value-v1', SHADOW, REAL);
    expect(c.firstDay).toBe('2026-09-02');
    expect(c.lastDay).toBe('2026-09-04');
    expect(c.shadowReturn).toBeCloseTo(0.05);
    // Real from 210k (Sep 2) to 220k (Sep 4), not from 200k.
    expect(c.realReturn).toBeCloseTo(220 / 210 - 1);
    expect(c.excess).toBeCloseTo(0.05 - (220 / 210 - 1));
    expect(c.daysElapsed).toBe(2);
    expect(c.drawdown).toBe(0);
    expect(c.maxDrawdown).toBe(-0.02);
  });

  it('leaves the real side empty without overlapping history', () => {
    const c = compareToReal('value-v1', SHADOW, []);
    expect(c.realReturn).toBeNull();
    expect(c.excess).toBeNull();
    expect(c.shadowReturn).toBeCloseTo(0.05);
  });
});

describe('comparisonSeries', () => {
  it('strategies starting day 1 and day 30 keep the day-1 history (UX-25)', () => {
    const late = [row('2026-09-03', 50_000), row('2026-09-04', 55_000)];
    const out = comparisonSeries(REAL, [
      { id: 'value-v1', rows: SHADOW },
      { id: 'late-v1', rows: late },
    ]);

    // Your portfolio starts at 100 on the earliest strategy's first day.
    expect(out.baseDay).toBe('2026-09-02');
    expect(out.real[0]).toEqual({ time: '2026-09-02', value: 100 });
    // The early strategy keeps all its days.
    expect(out.shadows[0].points.map((p) => p.time)).toEqual([
      '2026-09-02',
      '2026-09-03',
      '2026-09-04',
    ]);
    expect(out.shadows[0].points[0].value).toBe(100);
    expect(out.shadows[0].points[2].value).toBeCloseTo(105);
    // The late one starts on its own day, at your portfolio's value that day.
    const realOnSep3 = (220 / 210) * 100;
    expect(out.shadows[1].points[0].time).toBe('2026-09-03');
    expect(out.shadows[1].points[0].value).toBeCloseTo(realOnSep3);
    expect(out.shadows[1].points[1].value).toBeCloseTo(realOnSep3 * 1.1);
  });

  it('starts at 100 when your portfolio has no history', () => {
    const out = comparisonSeries([], [{ id: 'value-v1', rows: SHADOW }]);
    expect(out.real).toEqual([]);
    expect(out.shadows[0].points[0]).toEqual({ time: '2026-09-02', value: 100 });
  });

  it('is empty until a shadow strategy has P&L', () => {
    expect(comparisonSeries(REAL, [{ id: 'x', rows: [] }])).toEqual({
      baseDay: null,
      real: [],
      shadows: [],
    });
  });
});

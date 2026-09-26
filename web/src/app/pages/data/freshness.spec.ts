import { FRESHNESS_LABEL, FRESHNESS_TONE, freshnessOf, freshnessLimits } from './freshness';

const NOW = new Date('2026-09-26T12:00:00Z');

describe('freshnessOf', () => {
  it('calls a daily bar from yesterday fresh', () => {
    expect(freshnessOf('2026-09-25T00:00:00Z', '1d', NOW)).toBe('fresh');
  });

  it('keeps Friday bars fresh over a weekend', () => {
    // Friday close seen on Monday noon: 3.5 days.
    expect(freshnessOf('2026-09-25T00:00:00Z', '1d', new Date('2026-09-28T12:00:00Z'))).toBe(
      'fresh',
    );
  });

  it('calls a daily bar a week old stale', () => {
    expect(freshnessOf('2026-09-19T00:00:00Z', '1d', NOW)).toBe('stale');
  });

  it('calls a daily bar a month old old', () => {
    expect(freshnessOf('2026-08-26T00:00:00Z', '1d', NOW)).toBe('old');
  });

  it('gives weekly bars a longer window', () => {
    expect(freshnessOf('2026-09-19T00:00:00Z', '1w', NOW)).toBe('fresh');
    expect(freshnessOf('2026-09-06T00:00:00Z', '1w', NOW)).toBe('stale');
    expect(freshnessOf('2026-08-01T00:00:00Z', '1w', NOW)).toBe('old');
  });

  it('treats intraday like daily (markets close overnight and at weekends)', () => {
    expect(freshnessOf('2026-09-25T20:55:00Z', '5m', NOW)).toBe('fresh');
    expect(freshnessOf('2026-09-01T20:55:00Z', '5m', NOW)).toBe('old');
  });

  it('calls a missing or unreadable timestamp old', () => {
    expect(freshnessOf(null, '1d', NOW)).toBe('old');
    expect(freshnessOf(undefined, '1d', NOW)).toBe('old');
    expect(freshnessOf('not a date', '1d', NOW)).toBe('old');
  });

  it('calls a bar from the future fresh (clock skew)', () => {
    expect(freshnessOf('2026-09-27T00:00:00Z', '1d', NOW)).toBe('fresh');
  });

  it('exposes the limits it uses', () => {
    expect(freshnessLimits('1d')).toEqual({ freshDays: 4, staleDays: 10 });
    expect(freshnessLimits('1w')).toEqual({ freshDays: 10, staleDays: 21 });
  });

  it('maps each state to a pill tone and label', () => {
    expect(FRESHNESS_TONE).toEqual({ fresh: 'positive', stale: 'warn', old: 'negative' });
    expect(FRESHNESS_LABEL.old).toBe('Old');
  });
});

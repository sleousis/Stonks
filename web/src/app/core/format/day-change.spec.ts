import { activeFormat, browserFormat } from './format';
import { dayChangeLine, dayChangePercent, sessionLabel, sessionPhrase } from './day-change';

describe('day change', () => {
  const NOW = new Date('2026-09-28T12:00:00Z'); // a Monday

  beforeEach(() => activeFormat.set({ locale: 'en-US', timeZone: 'UTC', dateStyle: 'iso' }));
  afterEach(() => activeFormat.set(browserFormat()));

  it('names the session: today, a weekday this week, else the last session', () => {
    expect(sessionLabel('2026-09-28', NOW)).toBe('Today');
    expect(sessionLabel('2026-09-25', NOW)).toBe('Friday');
    expect(sessionLabel('2026-09-01', NOW)).toBe('Last session');
    expect(sessionPhrase('2026-09-25', NOW)).toBe('on Friday');
    expect(sessionPhrase('2026-09-28', NOW)).toBe('today');
  });

  it('writes one line, the same on Today, Dashboard and Insights (M2)', () => {
    expect(dayChangeLine(-6.09, -0.000609, '2026-09-24', 'USD', NOW)).toBe(
      '-$6.09 (-0.06%) on Thursday',
    );
    expect(dayChangeLine(1000, 0.01, '2026-09-28', 'USD', NOW)).toBe('+$1,000.00 (+1.00%) today');
    expect(dayChangeLine(null, null, null)).toBeNull();
  });

  it('always shows two decimals on the percent', () => {
    expect(dayChangePercent(-0.000609)).toBe('-0.06%');
    expect(dayChangePercent(0.1)).toBe('+10.00%');
  });
});

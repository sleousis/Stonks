import { MISSING, activeFormat, browserFormat, formatTime, formatWeekday } from './format';

describe('formatTime and formatWeekday', () => {
  afterEach(() => activeFormat.set(browserFormat()));

  it('shows a 24-hour clock time in the preferred zone', () => {
    activeFormat.set({ locale: 'en-US', timeZone: 'America/New_York', dateStyle: 'iso' });
    expect(formatTime('2026-09-26T13:30:00Z')).toBe('09:30');
    expect(formatTime('2026-09-26T20:05:00Z')).toBe('16:05');
    activeFormat.set({ locale: 'en-US', timeZone: 'Asia/Tokyo', dateStyle: 'iso' });
    expect(formatTime('2026-09-26T20:05:00Z')).toBe('05:05');
  });

  it('names the weekday in the locale', () => {
    activeFormat.set({ locale: 'en-US', timeZone: 'UTC', dateStyle: 'iso' });
    expect(formatWeekday('2026-09-28T13:30:00Z')).toBe('Mon');
  });

  it('copes with missing or bad input', () => {
    expect(formatTime(null)).toBe(MISSING);
    expect(formatTime('soon')).toBe('soon');
    expect(formatWeekday(undefined)).toBe(MISSING);
  });
});

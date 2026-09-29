import { activeFormat, browserFormat, formatMoney, formatNumber, formatPercent } from './format';

describe('figures that round to zero', () => {
  beforeEach(() => activeFormat.set({ locale: 'en-US', timeZone: 'UTC', dateStyle: 'iso' }));
  afterEach(() => activeFormat.set(browserFormat()));

  it('never shows a negative zero', () => {
    expect(formatPercent(-0.00001)).toBe('0.00%');
    expect(formatPercent(-0.0004, { digits: 1 })).toBe('0.0%');
    expect(formatMoney(-0.001)).toBe('$0.00');
    expect(formatNumber(-0.00001, { digits: 2 })).toBe('0');
    expect(formatPercent(-0)).toBe('0.00%');
  });

  it('never puts a plus on a zero', () => {
    expect(formatPercent(0.00001, { signed: true })).toBe('0.00%');
    expect(formatMoney(0.001, { signed: true })).toBe('$0.00');
  });

  it('keeps real signs', () => {
    expect(formatPercent(-0.0123, { signed: true })).toBe('-1.23%');
    expect(formatPercent(0.0123, { signed: true })).toBe('+1.23%');
    expect(formatMoney(-12.5)).toBe('-$12.50');
  });
});

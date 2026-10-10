import { activeFormat, browserFormat, formatMoney, formatNumber } from './format';

describe('formatNumber defaults', () => {
  beforeEach(() => activeFormat.set({ locale: 'en-US', timeZone: 'UTC', dateStyle: 'iso' }));
  afterEach(() => activeFormat.set(browserFormat()));

  it('shows at most 2 decimals by default', () => {
    expect(formatNumber(9.008534)).toBe('9.01');
    expect(formatNumber(3.3634)).toBe('3.36');
    expect(formatNumber(1234)).toBe('1,234');
  });

  it('shows at most 1 decimal when compact', () => {
    expect(formatNumber(61_489_800, { compact: true })).toBe('61.5M');
    expect(formatNumber(1_234, { compact: true })).toBe('1.2K');
  });

  it('keeps an explicit digits choice', () => {
    expect(formatNumber(1.23456, { digits: 4 })).toBe('1.2346');
    expect(formatNumber(1.6, { digits: 0 })).toBe('2');
  });

  it('never rounds a tiny real value to zero', () => {
    expect(formatNumber(0.0015)).toBe('0.0015');
    expect(formatNumber(-0.00012)).toBe('-0.00012');
  });

  it('uses capital K, M and B in English locales', () => {
    activeFormat.set({ locale: 'en-GB', timeZone: 'UTC', dateStyle: 'iso' });
    expect(formatNumber(61_489_800, { compact: true })).toBe('61.5M');
    expect(formatNumber(2_500, { compact: true })).toBe('2.5K');
    expect(formatNumber(3_200_000_000, { compact: true })).toBe('3.2B');
    expect(formatMoney(100_000, { compact: true })).toBe('US$100K');
  });
});

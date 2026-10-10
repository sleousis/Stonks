import { activeFormat, browserFormat } from '../../core/format/format';
import { axisFormatter } from './axis-format';

describe('axisFormatter', () => {
  beforeEach(() => activeFormat.set({ locale: 'en-US', timeZone: 'UTC', dateStyle: 'iso' }));
  afterEach(() => activeFormat.set(browserFormat()));

  it('uses compact money for every label when the range reaches 100k', () => {
    const fmt = axisFormatter('money', [99_000, 101_000]);
    expect(fmt(99_000)).toBe('$99K');
    expect(fmt(100_000)).toBe('$100K');
  });

  it('uses full money with no cents for a mid range', () => {
    const fmt = axisFormatter('money', [9_000, 12_000]);
    expect(fmt(9_500)).toBe('$9,500');
    expect(fmt(10_250.4)).toBe('$10,250');
  });

  it('keeps cents for small prices', () => {
    const fmt = axisFormatter('money', [12, 14.5]);
    expect(fmt(12.5)).toBe('$12.50');
  });

  it('formats plain numbers once per axis', () => {
    const fmt = axisFormatter('number', [50_000, 250_000]);
    expect(fmt(50_000)).toBe('50K');
    expect(fmt(1.2345)).toBe('1.2');
    expect(axisFormatter('number', [0.5, 2])(1.2345)).toBe('1.23');
  });

  it('formats percent with one decimal', () => {
    expect(axisFormatter('percent', [-0.1, 0.2])(0.123)).toBe('12.3%');
  });
});

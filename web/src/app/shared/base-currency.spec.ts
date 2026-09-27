import { baseCurrencyLine } from './base-currency';

describe('baseCurrencyLine', () => {
  it('shows the value in the base currency only when it differs', () => {
    expect(baseCurrencyLine({ currency: 'USD', total_value_base: 9120 }, 'EUR')).toBe(
      'Value in EUR: €9,120.00',
    );
    expect(baseCurrencyLine({ currency: 'EUR', total_value_base: 9120 }, 'EUR')).toBeNull();
    expect(baseCurrencyLine({ currency: 'USD', total_value_base: null }, 'EUR')).toBeNull();
    expect(baseCurrencyLine({ currency: 'USD', total_value_base: 1 }, null)).toBeNull();
  });

  it('says which rate is missing instead of guessing', () => {
    expect(
      baseCurrencyLine({ currency: 'USD', total_value_base: null, fx_missing: ['GBP'] }, 'EUR'),
    ).toBe('No exchange rate yet for GBP, so there is no total in EUR.');
  });
});

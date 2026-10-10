import {
  assetClassLabel,
  commonIntervals,
  exchangeLabel,
  instrumentClassLabel,
  intervalLabel,
  kindLabel,
  runStatusLabel,
  sourceLabel,
  updateOutcome,
} from './data-labels';

describe('data labels', () => {
  it('shows the demo source as Demo data', () => {
    expect(sourceLabel('fake')).toBe('Demo data');
  });

  it('names exchanges, and keeps a code it does not know', () => {
    expect(exchangeLabel('BR')).toBe('Brussels');
    expect(exchangeLabel('AS')).toBe('Amsterdam');
    expect(exchangeLabel('CC')).toBe('Crypto');
    expect(exchangeLabel('US')).toBe('US');
    expect(exchangeLabel('ZZ')).toBe('ZZ');
    expect(exchangeLabel(null)).toBe('');
  });

  it('calls funds Fund (ETF), not Stocks', () => {
    expect(instrumentClassLabel({ asset_class: 'equity', security_type: 'etf' })).toBe(
      'Fund (ETF)',
    );
    expect(instrumentClassLabel({ asset_class: 'equity', security_type: 'fund' })).toBe(
      'Fund (ETF)',
    );
    expect(instrumentClassLabel({ asset_class: 'equity', security_type: 'common_stock' })).toBe(
      'Stocks',
    );
    expect(instrumentClassLabel({ asset_class: 'crypto' })).toBe('Crypto');
  });

  it('offers the common intervals only, plus the one in use', () => {
    const all = ['1m', '2m', '5m', '15m', '30m', '1h', '4h', '1d', '1w', '1mo', '3mo', '1y'];
    expect(commonIntervals(all, '1d')).toEqual(['1m', '5m', '15m', '1h', '1d', '1w', '1mo']);
    expect(commonIntervals(all, '4h')).toContain('4h');
    expect(commonIntervals(['1d'], '1d')).toEqual(['1d']);
    expect(intervalLabel('1d')).toBe('Daily');
    expect(intervalLabel('15m')).toBe('15 minutes');
    expect(intervalLabel('4h')).toBe('4h');
  });

  it('names providers, kinds and outcomes in trader words', () => {
    expect(sourceLabel('eodhd')).toBe('EODHD');
    expect(sourceLabel('yahoo')).toBe('Yahoo Finance');
    expect(sourceLabel('new_vendor')).toBe('New vendor');
    expect(kindLabel('prices')).toBe('Daily prices');
    expect(kindLabel('metadata')).toBe('Company details');
    expect(runStatusLabel('ok')).toBe('Done');
    expect(runStatusLabel('partial')).toBe('Partly done');
    expect(runStatusLabel('error')).toBe('Failed');
  });

  it('names asset classes in words, never the lower-case id', () => {
    expect(assetClassLabel('equity')).toBe('Stocks');
    expect(assetClassLabel('crypto')).toBe('Crypto');
    expect(assetClassLabel('commodity')).toBe('Commodities');
    expect(assetClassLabel('bond')).toBe('Bonds');
    expect(assetClassLabel(null)).toBe('');
  });

  it('says what an update did, plainly', () => {
    expect(updateOutcome({ tickers_ok: 2, tickers_failed: 0 })).toBe('Updated 2 tickers.');
    expect(updateOutcome({ tickers_ok: 1, tickers_failed: 1 })).toBe(
      'Updated 1 ticker. 1 ticker failed.',
    );
    expect(updateOutcome({ tickers_ok: 0, tickers_failed: 3 })).toBe('Could not update 3 tickers.');
  });
});

import { algoText, parseTargets, skipText } from './rebalance.page';

describe('rebalance helpers', () => {
  it('parses weights as fractions or percents', () => {
    expect(parseTargets('aapl.us=30%\nMSFT.US=0.2, ')).toEqual([
      { ticker: 'AAPL.US', weight: 0.3 },
      { ticker: 'MSFT.US', weight: 0.2 },
    ]);
  });

  it('names a line it cannot read', () => {
    expect(parseTargets('AAPL.US')).toBe('"AAPL.US" is not TICKER=WEIGHT');
    expect(parseTargets('AAPL.US=-1')).toBe('"AAPL.US=-1" is not TICKER=WEIGHT');
  });

  it('says why a line has no trade', () => {
    expect(skipText('below_one_share')).toBe('Less than one share');
    expect(skipText(null)).toBe('');
  });

  it('describes the algo setting in plain words', () => {
    expect(algoText(null)).toBe('Plain limit orders');
    expect(algoText({ algo: 'adaptive', params: { priority: 'patient' } })).toBe(
      'Adaptive, patient',
    );
    expect(algoText({ algo: 'vwap', params: { start_minutes: 0, end_minutes: 90 } })).toBe(
      'VWAP, 0 to 90 min after the open',
    );
  });
});

import { kindLabel, runStatusLabel, sourceLabel, updateOutcome } from './data-labels';

describe('data labels', () => {
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

  it('says what an update did, plainly', () => {
    expect(updateOutcome({ tickers_ok: 2, tickers_failed: 0 })).toBe('Updated 2 tickers.');
    expect(updateOutcome({ tickers_ok: 1, tickers_failed: 1 })).toBe(
      'Updated 1 ticker. 1 ticker failed.',
    );
    expect(updateOutcome({ tickers_ok: 0, tickers_failed: 3 })).toBe('Could not update 3 tickers.');
  });
});

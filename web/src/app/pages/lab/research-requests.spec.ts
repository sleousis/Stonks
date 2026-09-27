import {
  buildSignalIcRequest,
  buildSweepRequest,
  defaultSignalIcForm,
  defaultSweepForm,
  parseHorizons,
  signalIcErrors,
  sweepErrors,
} from './research-requests';

const TODAY = new Date(2026, 8, 26);

describe('sweep requests', () => {
  it('sends tickers, the suite and the search, and every strategy by default', () => {
    const f = { ...defaultSweepForm(TODAY), tickers: 'spy.us qqq.us' };
    expect(sweepErrors(f)).toEqual({});
    expect(buildSweepRequest(f)).toEqual({
      start: '2025-09-26',
      end: '2026-09-26',
      interval: '1d',
      universe: ['SPY.US', 'QQQ.US'],
      preset: 'quick',
      tuner: 'random',
      budget: 20,
      objective: 'sharpe',
    });
  });

  it('sends a universe id and picked strategies instead', () => {
    const f = {
      ...defaultSweepForm(TODAY),
      basket: 'universe' as const,
      universeId: 'sp500',
      strategies: ['pkg.mod:Momentum'],
    };
    const body = buildSweepRequest(f);
    expect(body.universe_id).toBe('sp500');
    expect(body.universe).toBeUndefined();
    expect(body.strategies).toEqual(['pkg.mod:Momentum']);
  });

  it('says what is missing', () => {
    expect(sweepErrors(defaultSweepForm(TODAY))).toEqual({
      tickers: 'Enter at least one ticker.',
    });
    expect(sweepErrors({ ...defaultSweepForm(TODAY), basket: 'universe', budget: 0 })).toEqual({
      universe: 'Pick a universe.',
      budget: 'Between 1 and 1000.',
    });
  });
});

describe('signal IC requests', () => {
  it('sends the strategy, tickers and window, horizons only when typed', () => {
    const f = { ...defaultSignalIcForm(TODAY), classPath: 'pkg.mod:Momentum', tickers: 'aapl.us' };
    expect(signalIcErrors(f)).toEqual({});
    expect(buildSignalIcRequest(f)).toEqual({
      strategy: { class_path: 'pkg.mod:Momentum' },
      universe: ['AAPL.US'],
      start: '2025-09-26',
      end: '2026-09-26',
      interval: '1d',
    });
    expect(buildSignalIcRequest({ ...f, horizons: '21, 1 5' }).horizons).toEqual([1, 5, 21]);
  });

  it('checks horizons', () => {
    expect(parseHorizons('1, 600')).toBeNull();
    const f = { ...defaultSignalIcForm(TODAY), classPath: 'x:Y', tickers: 'A', horizons: '1.5' };
    expect(signalIcErrors(f)['horizons']).toContain('Whole numbers of bars');
  });
});

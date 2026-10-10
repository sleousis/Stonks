import {
  SIMPLE_SUITE,
  earliestCommonStart,
  simpleTestErrors,
  simpleTestRequest,
} from './simple-test-form';
import { presetTickers } from './strategy-preset';

const TODAY = new Date(Date.UTC(2026, 8, 28));

describe('simple test form', () => {
  const base = {
    classPath: 'a.b:Momentum',
    tickers: 'spy.us qqq.us',
    universeId: '',
    start: '2025-09-28',
    end: '2026-09-28',
  };

  it('sends a Standard lab run with default settings on daily bars', () => {
    const body = simpleTestRequest(base, TODAY);
    expect(SIMPLE_SUITE).toBe('standard');
    expect(body).toMatchObject({
      strategy: { class_path: 'a.b:Momentum' },
      universe: ['SPY.US', 'QQQ.US'],
      interval: '1d',
      preset: 'standard',
      tuner: 'random',
      budget: 20,
    });
    // Never on trial, never custom tests.
    expect(body.register_if_passes).toBeUndefined();
    expect(body.register_strategy).toBeUndefined();
    expect(body.survival_tests).toBeUndefined();
  });

  it('runs on a saved list and fetches its missing prices first', () => {
    const body = simpleTestRequest({ ...base, tickers: '', universeId: 'us-big' }, TODAY);
    expect(body).toMatchObject({ universe_id: 'us-big', ensure_data: true });
    expect(body.universe).toBeUndefined();
  });

  it('says what is missing in plain words', () => {
    expect(simpleTestErrors({ ...base, classPath: '', tickers: '' })).toEqual({
      strategy: 'Pick a strategy.',
      tickers: 'Enter at least one ticker.',
    });
    expect(simpleTestErrors({ ...base, start: '2026-09-28', end: '2026-01-01' })).toEqual({
      end: 'End must be after start.',
    });
    // A saved list replaces the tickers.
    expect(simpleTestErrors({ ...base, tickers: '', universeId: 'us-big' })).toEqual({});
  });

  it('records the strategy idea as the hypothesis when given', () => {
    expect(simpleTestRequest(base, TODAY, 'Trends persist.').hypothesis).toBe('Trends persist.');
    expect(simpleTestRequest(base, TODAY).hypothesis).toBeUndefined();
  });

  it('starts on the first day every ticker has prices (R15)', () => {
    expect(earliestCommonStart(['2024-01-02T00:00:00', '2024-03-01T00:00:00'])).toBe('2024-03-01');
    expect(earliestCommonStart(['2024-01-02', null])).toBeNull();
    expect(earliestCommonStart([])).toBeNull();
  });

  it("reads a saved strategy's own tickers (NB-09)", () => {
    expect(presetTickers({ ticker: 'spy.us', allocation: 1 })).toBe('SPY.US');
    expect(presetTickers({ tickers: ['AAPL.US', 'msft.us'] })).toBe('AAPL.US, MSFT.US');
    expect(presetTickers({ lookback_days: 20 })).toBe('');
  });
});

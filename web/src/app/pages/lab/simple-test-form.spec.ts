import { SIMPLE_SUITE, simpleTestErrors, simpleTestRequest } from './simple-test-form';

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
});

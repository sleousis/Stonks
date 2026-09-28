import type { WatchlistView } from '../../../api/models';
import {
  FACTOR_STRATEGY_CLASS,
  basketError,
  basketFields,
  basketText,
  buildFactorRunRequest,
  buildTearsheetRequest,
  buildValuesRequest,
  defaultBasket,
  defaultFactorRunForm,
  defaultTearsheetForm,
  directionText,
  divergingColor,
  factorRunErrors,
  matchesQuery,
  maxMagnitude,
  tearsheetErrors,
  warmupText,
} from './factor-requests';
import { MOM } from './factor-test-fixtures';

const LIST: WatchlistView = {
  id: 'wl1',
  name: 'Tech',
  tickers: ['AAPL.US', 'MSFT.US'],
  created_at: '2026-09-01T00:00:00Z',
  updated_at: '2026-09-01T00:00:00Z',
};

const TODAY = new Date(Date.UTC(2026, 8, 27));

describe('factor baskets', () => {
  it('asks for a universe, watchlist or tickers', () => {
    expect(basketError(defaultBasket())).toBe('Pick a universe.');
    expect(basketError({ ...defaultBasket(), kind: 'watchlist' })).toBe('Pick a watchlist.');
    expect(
      basketError({ ...defaultBasket(), kind: 'watchlist', watchlistId: 'wl1' }, [
        { ...LIST, tickers: [] },
      ]),
    ).toBe('This watchlist has no tickers.');
    expect(basketError({ ...defaultBasket(), kind: 'tickers', tickers: ' , ' })).toBe(
      'Enter at least one ticker.',
    );
    expect(basketError({ ...defaultBasket(), universeId: 'sp500' })).toBeNull();
  });

  it('sends a universe id, or the watchlist or typed tickers', () => {
    expect(basketFields({ ...defaultBasket(), universeId: 'sp500' })).toEqual({
      universe_id: 'sp500',
    });
    expect(
      basketFields({ ...defaultBasket(), kind: 'watchlist', watchlistId: 'wl1' }, [LIST]),
    ).toEqual({ universe: ['AAPL.US', 'MSFT.US'] });
    expect(
      basketFields({ ...defaultBasket(), kind: 'tickers', tickers: 'aapl.us nvda.us' }),
    ).toEqual({ universe: ['AAPL.US', 'NVDA.US'] });
  });

  it('names the basket for confirmations', () => {
    expect(basketText({ ...defaultBasket(), universeId: 'sp500' })).toBe('the sp500 universe');
    expect(basketText({ ...defaultBasket(), kind: 'watchlist', watchlistId: 'wl1' }, [LIST])).toBe(
      'the Tech watchlist',
    );
    expect(basketText({ ...defaultBasket(), kind: 'tickers', tickers: 'A.US' })).toBe('1 ticker');
  });
});

describe('factor requests', () => {
  it('builds a values request', () => {
    expect(
      buildValuesRequest('low_vol_60', '2026-09-25', { ...defaultBasket(), universeId: 'sp500' }),
    ).toEqual({ factor: 'low_vol_60', as_of: '2026-09-25', universe_id: 'sp500' });
  });

  it('checks and builds a tear sheet request', () => {
    const form = {
      ...defaultTearsheetForm(TODAY),
      basket: { ...defaultBasket(), universeId: 'x' },
    };
    expect(form.start).toBe('2023-09-27');
    expect(tearsheetErrors(form)).toEqual({});
    expect(buildTearsheetRequest('mom_12_1', { ...form, horizons: '21, 1, 5, 5' })).toEqual({
      factor: 'mom_12_1',
      start: '2023-09-27',
      end: '2026-09-27',
      n_quantiles: 5,
      universe_id: 'x',
      horizons: [1, 5, 21],
    });
    expect(buildTearsheetRequest('f', { ...form, horizons: ' ' }).horizons).toBeUndefined();
    expect(
      tearsheetErrors({ ...form, horizons: '0, 5', quantiles: 1, end: form.start }),
    ).toMatchObject({
      horizons: expect.stringContaining('1 to 504'),
      quantiles: 'From 2 to 20.',
      end: 'End must be after start.',
    });
  });

  it('starts a library factor run from its hypothesis and a formula blank', () => {
    expect(defaultFactorRunForm(MOM, TODAY).hypothesis).toBe(MOM.hypothesis);
    const blank = defaultFactorRunForm(null, TODAY);
    expect(blank.hypothesis).toBe('');
    expect(blank.start).toBe('2021-09-27');
    expect(factorRunErrors(blank)).toMatchObject({
      basket: 'Pick a universe.',
      hypothesis: expect.stringContaining('Say why'),
    });
  });

  it('pins the factor on a factor strategy lab run', () => {
    const form = {
      ...defaultFactorRunForm(MOM, TODAY),
      basket: { ...defaultBasket(), kind: 'tickers' as const, tickers: 'a.us b.us' },
      suite: 'standard' as const,
    };
    expect(factorRunErrors(form)).toEqual({});
    expect(buildFactorRunRequest('$close / Ref($close, 20)', FACTOR_STRATEGY_CLASS, form)).toEqual({
      strategy: {
        class_path: FACTOR_STRATEGY_CLASS,
        params: { factor: '$close / Ref($close, 20)' },
      },
      start: '2021-09-27',
      end: '2026-09-27',
      interval: '1d',
      tuner: 'random',
      budget: 20,
      objective: 'sharpe',
      preset: 'standard',
      hypothesis: MOM.hypothesis,
      universe: ['A.US', 'B.US'],
    });
  });
});

describe('factor display helpers', () => {
  it('words the direction and warm-up', () => {
    expect(directionText(1)).toBe('Higher is better');
    expect(directionText(-1)).toBe('Lower is better');
    expect(warmupText(0)).toBe('None');
    expect(warmupText(1)).toBe('1 bar');
    expect(warmupText(252)).toBe('252 bars');
    expect(warmupText(null)).toBe('n/a');
  });

  it('searches id, family, description and formula', () => {
    expect(matchesQuery(MOM, '')).toBe(true);
    expect(matchesQuery(MOM, 'MOMENTUM')).toBe(true);
    expect(matchesQuery(MOM, 'ref($close')).toBe(true);
    expect(matchesQuery(MOM, 'piotroski')).toBe(false);
  });

  it('colours signed values on a diverging scale', () => {
    expect(maxMagnitude([0.02, null, -0.05, Number.NaN])).toBe(0.05);
    expect(divergingColor(null, 1)).toBeNull();
    expect(divergingColor(0.1, 0)).toBeNull();
    expect(divergingColor(-0.05, 0.05)).toContain('var(--color-loss) 60%');
    expect(divergingColor(0.025, 0.05)).toContain('var(--color-gain) 30%');
  });
});

import {
  addDays,
  comparisonLabel,
  importanceLabel,
  daysBetween,
  parseCountries,
  parseTickers,
  safeUrl,
  scopeQuery,
  sentimentWord,
  summarizeSentiment,
  timingLabel,
  windowError,
} from './calendar-view';

describe('calendar view helpers', () => {
  it('reads typed tickers in upper case, once each', () => {
    expect(parseTickers(' aapl.us, msft.us\nAAPL.US;nvda.us ')).toEqual([
      'AAPL.US',
      'MSFT.US',
      'NVDA.US',
    ]);
    expect(parseTickers('')).toEqual([]);
  });

  it('keeps only country codes', () => {
    expect(parseCountries('us, de, eu, United States')).toEqual(['US', 'DE', 'EU']);
  });

  it('builds the scope part of the query', () => {
    expect(scopeQuery({ scope: 'holdings', watchlistId: null, tickers: [] })).toEqual({
      scope: 'holdings',
    });
    expect(scopeQuery({ scope: 'watchlists', watchlistId: 'wl_1', tickers: [] })).toEqual({
      scope: 'watchlists',
      watchlist_id: 'wl_1',
    });
    expect(scopeQuery({ scope: 'watchlists', watchlistId: null, tickers: [] })).toEqual({
      scope: 'watchlists',
    });
    expect(scopeQuery({ scope: 'tickers', watchlistId: null, tickers: ['A.US', 'B.US'] })).toEqual({
      scope: 'tickers',
      tickers: 'A.US,B.US',
    });
    // Nothing to ask until some tickers are named.
    expect(scopeQuery({ scope: 'tickers', watchlistId: null, tickers: [] })).toBeNull();
    expect(scopeQuery({ scope: 'all', watchlistId: 'wl_1', tickers: ['A.US'] })).toEqual({
      scope: 'all',
    });
  });

  it('moves dates by whole days across month ends', () => {
    expect(addDays('2026-09-27', 30)).toBe('2026-10-27');
    expect(addDays('2026-03-01', -1)).toBe('2026-02-28');
    expect(daysBetween('2026-09-27', '2026-10-27')).toBe(30);
  });

  it('checks the window', () => {
    expect(windowError('2026-09-27', '2026-10-27')).toBeNull();
    expect(windowError('2026-10-27', '2026-09-27')).toBe('The end comes before the start.');
    expect(windowError('2026-01-01', '2026-12-31')).toBe('Pick at most 120 days.');
    expect(windowError('', '2026-12-31')).toBe('Pick both dates.');
  });

  it('names the time of a report and the basis of a release', () => {
    expect(timingLabel('before')).toBe('Before the open');
    expect(timingLabel('after')).toBe('After the close');
    expect(timingLabel('during')).toBe('During the day');
    expect(timingLabel(null)).toBe('Time not given');
    expect(comparisonLabel('yoy')).toBe('Year on year');
    expect(comparisonLabel('none')).toBe('Level');
    expect(importanceLabel('high')).toBe('High');
    expect(importanceLabel('medium')).toBe('Medium');
    expect(importanceLabel('low')).toBe('Low');
  });

  it('puts sentiment in words', () => {
    expect(sentimentWord(0.4)).toBe('Positive');
    expect(sentimentWord(-0.3)).toBe('Negative');
    expect(sentimentWord(0.05)).toBe('Neutral');
    expect(sentimentWord(null)).toBe('No score');
  });

  it('sums sentiment per ticker, weighted by articles', () => {
    const rows = summarizeSentiment([
      { ticker: 'B.US', day: '2026-09-25', sentiment: 0.5, article_count: 1 },
      { ticker: 'A.US', day: '2026-09-25', sentiment: 0.2, article_count: 3 },
      { ticker: 'A.US', day: '2026-09-26', sentiment: -0.6, article_count: 1 },
      { ticker: 'A.US', day: '2026-09-27', sentiment: null, article_count: 2 },
    ]);
    expect(rows.map((r) => r.ticker)).toEqual(['A.US', 'B.US']);
    const a = rows[0];
    expect(a.latest).toBe(-0.6);
    expect(a.latestDay).toBe('2026-09-26');
    expect(a.average).toBeCloseTo((0.2 * 3 - 0.6) / 4);
    expect(a.articles).toBe(6);
    expect(a.days).toBe(3);
  });

  it('lets only web links out', () => {
    expect(safeUrl('https://example.com/a')).toBe('https://example.com/a');
    expect(safeUrl('javascript:alert(1)')).toBeNull();
    expect(safeUrl(null)).toBeNull();
  });
});

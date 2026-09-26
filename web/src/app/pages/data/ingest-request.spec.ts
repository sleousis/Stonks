import {
  EMPTY_INGEST_FORM,
  type IngestFormValue,
  buildIngestRequest,
  describeIngest,
  ingestProblems,
  parseTickers,
} from './ingest-request';

function form(patch: Partial<IngestFormValue>): IngestFormValue {
  return { ...EMPTY_INGEST_FORM, ...patch };
}

describe('parseTickers', () => {
  it('splits on commas, spaces and new lines, upper-cases and de-duplicates', () => {
    expect(parseTickers(' aapl.us, MSFT.US\nnvda.us  aapl.us ;; ')).toEqual([
      'AAPL.US',
      'MSFT.US',
      'NVDA.US',
    ]);
  });

  it('returns nothing for blank input', () => {
    expect(parseTickers('  , \n')).toEqual([]);
  });
});

describe('buildIngestRequest', () => {
  it('builds a prices request with only the fields that are set', () => {
    expect(
      buildIngestRequest(form({ kind: 'prices', tickers: 'aapl.us msft.us', since: '2026-01-02' })),
    ).toEqual({
      source: 'eodhd',
      kind: 'prices',
      tickers: ['AAPL.US', 'MSFT.US'],
      since: '2026-01-02',
    });
  });

  it('keeps the source, until and exchange', () => {
    expect(
      buildIngestRequest(
        form({ source: 'yahoo', kind: 'prices', exchange: ' us ', until: '2026-02-01' }),
      ),
    ).toEqual({
      source: 'yahoo',
      kind: 'prices',
      tickers: [],
      exchange: 'US',
      until: '2026-02-01',
    });
  });

  it('sends the interval only for intraday', () => {
    expect(
      buildIngestRequest(form({ kind: 'intraday', tickers: 'x', interval: '5m' })).interval,
    ).toBe('5m');
    expect(
      buildIngestRequest(form({ kind: 'prices', tickers: 'x', interval: '5m' })),
    ).not.toHaveProperty('interval');
  });

  it('drops the exchange for kinds other than prices', () => {
    expect(
      buildIngestRequest(form({ kind: 'fundamentals', tickers: 'x', exchange: 'US' })),
    ).not.toHaveProperty('exchange');
  });
});

describe('ingestProblems', () => {
  it('passes a complete request', () => {
    expect(ingestProblems(form({ tickers: 'AAPL.US' }))).toEqual([]);
  });

  it('needs tickers or an exchange', () => {
    expect(ingestProblems(form({}))).toEqual(['Enter at least one ticker, or an exchange.']);
    expect(ingestProblems(form({ exchange: 'US' }))).toEqual([]);
  });

  it('needs an interval for intraday', () => {
    expect(ingestProblems(form({ kind: 'intraday', tickers: 'X' }))).toContain(
      'Choose an interval for intraday bars.',
    );
  });

  it('rejects since after until', () => {
    expect(
      ingestProblems(form({ tickers: 'X', since: '2026-03-01', until: '2026-02-01' })),
    ).toContain('"Since" must be on or before "until".');
  });
});

describe('describeIngest', () => {
  it('says what will be fetched, from where and from when', () => {
    expect(
      describeIngest({ source: 'eodhd', kind: 'prices', tickers: ['A', 'B'], since: '2026-01-02' }),
    ).toBe('Fetches prices for 2 tickers (A, B) from eodhd since 2026-01-02 and saves them.');
  });

  it('describes exchange discovery and intraday intervals', () => {
    expect(describeIngest({ source: 'eodhd', kind: 'prices', tickers: [], exchange: 'US' })).toBe(
      'Fetches prices for every ticker on US from eodhd and saves them.',
    );
    expect(
      describeIngest({ source: 'yahoo', kind: 'intraday', tickers: ['A'], interval: '5m' }),
    ).toBe('Fetches 5m intraday bars for 1 ticker (A) from yahoo and saves them.');
  });
});

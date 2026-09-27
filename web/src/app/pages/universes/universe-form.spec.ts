import {
  DEFAULT_FIELDS,
  type UniverseForm,
  parseSpec,
  specFromFields,
  specTemplate,
  universeCreateBody,
  formFromUniverse,
  universeFormErrors,
  universeUpdateBody,
} from './universe-form';
import { type ScreenForm, filterRow } from '../screener/screen-form';
import { METRICS } from '../../../testing/screener-fixtures';

function form(over: Partial<UniverseForm> = {}): UniverseForm {
  return {
    id: 'big-caps',
    name: '',
    description: '',
    kind: 'list',
    source: 'fields',
    fields: { ...DEFAULT_FIELDS, tickers: 'aapl.us', startDate: '' },
    specText: '{"tickers": ["AAPL.US"]}',
    csv: '',
    ...over,
  };
}

describe('universe form', () => {
  it('starts each kind from an example spec', () => {
    expect(JSON.parse(specTemplate('exchange'))).toMatchObject({ exchange: 'US' });
    expect(JSON.parse(specTemplate('index'))).toMatchObject({ index_id: 'sp500' });
  });

  it('builds the spec from each kind own fields', () => {
    const f = DEFAULT_FIELDS;
    expect(specFromFields('list', { ...f, tickers: 'aapl.us msft.us, aapl.us' })).toEqual({
      tickers: ['AAPL.US', 'MSFT.US'],
      start_date: '2020-01-01',
    });
    expect(specFromFields('exchange', { ...f, exchange: 'lse', includeDelisted: false })).toEqual({
      exchange: 'LSE',
      source: 'eodhd',
      include_delisted: false,
      start_date: '2020-01-01',
    });
    const screen: ScreenForm = { ...f.screen, minPrice: '', assetClasses: ['equity', 'crypto'] };
    expect(specFromFields('rule', { ...f, screen })).toEqual({
      rebalance: 'monthly',
      start: '2020-01-01',
      end: null,
      min_adv: 1000000,
      asset_classes: ['equity', 'crypto'],
    });
    // The rule's screen takes the screener's metric filters, in percent for percent metrics.
    const filters = [filterRow('dividend_yield', '3', '')];
    expect(
      specFromFields(
        'rule',
        { ...f, screen: { ...f.screen, filters, sortBy: 'price', limit: '20' } },
        METRICS,
      ),
    ).toMatchObject({
      filters: [{ metric: 'dividend_yield', min: 0.03, max: null }],
      sort_by: 'price',
      descending: true,
      limit: 20,
    });
    expect(specFromFields('index', f)).toEqual({
      index_id: 'sp500',
      source: 'wikipedia_sp500',
      start_date: '2020-01-01',
    });
  });

  it('parses a JSON definition and explains bad ones', () => {
    expect(parseSpec('{"a": 1}')).toEqual({ spec: { a: 1 } });
    expect(parseSpec('')).toEqual({ spec: {} });
    expect(parseSpec('[1]')).toEqual({ error: expect.stringContaining('JSON object') });
    expect(parseSpec('{nope')).toEqual({ error: 'The definition is not valid JSON.' });
  });

  it('checks the id, the fields, the JSON and the CSV', () => {
    expect(universeFormErrors(form())).toEqual({});
    expect(universeFormErrors(form({ id: '' })).id).toBe('Enter an id.');
    expect(universeFormErrors(form({ id: 'has space' })).id).toContain('letters');
    expect(universeFormErrors(form({ fields: { ...DEFAULT_FIELDS, tickers: ' ' } })).tickers).toBe(
      'Enter at least one ticker.',
    );
    const lots = { ...DEFAULT_FIELDS, screen: { ...DEFAULT_FIELDS.screen, minAdv: 'lots' } };
    expect(universeFormErrors(form({ kind: 'rule', fields: lots })).screen).toContain(
      'dollar volume',
    );
    const late = { ...DEFAULT_FIELDS, start: '2026-01-02', end: '2025-01-02' };
    expect(universeFormErrors(form({ kind: 'rule', fields: late })).window).toBeDefined();
    const empty = {
      ...DEFAULT_FIELDS,
      screen: { ...DEFAULT_FIELDS.screen, filters: [filterRow()] },
    };
    expect(universeFormErrors(form({ kind: 'rule', fields: empty })).screen).toContain(
      'Pick a metric',
    );
    expect(universeFormErrors(form({ source: 'json', specText: '{' })).spec).toBeDefined();
    expect(universeFormErrors(form({ source: 'csv' })).csv).toBe('Choose a CSV file.');
  });

  it('sends the fields, the JSON, or a CSV with an empty spec', () => {
    expect(universeCreateBody(form({ name: ' Big ' }))).toEqual({
      id: 'big-caps',
      kind: 'list',
      name: 'Big',
      description: null,
      spec: { tickers: ['AAPL.US'] },
      csv: null,
    });
    expect(universeCreateBody(form({ source: 'json', specText: '{"x": 1}' })).spec).toEqual({
      x: 1,
    });
    expect(universeCreateBody(form({ source: 'csv', csv: 'ticker\nAAPL.US' }))).toMatchObject({
      spec: {},
      csv: 'ticker\nAAPL.US',
    });
  });

  it('reads a stored definition back into the fields, or JSON when fields cannot hold it', () => {
    const list = formFromUniverse({
      id: 'big',
      kind: 'list',
      name: 'Big',
      spec: { tickers: ['AAPL.US', 'MSFT.US'], start_date: '1900-01-01' },
    });
    expect(list.source).toBe('fields');
    expect(list.fields.tickers).toBe('AAPL.US, MSFT.US');
    // The open-ended start shows blank.
    expect(list.fields.startDate).toBe('');
    expect(universeUpdateBody(list)).toEqual({
      kind: 'list',
      name: 'Big',
      description: null,
      spec: { tickers: ['AAPL.US', 'MSFT.US'] },
      csv: null,
    });

    const spans = formFromUniverse({
      id: 'dated',
      kind: 'list',
      spec: { spans: [{ ticker: 'OLD.US', start_date: '2020-01-02' }] },
    });
    expect(spans.source).toBe('json');
    expect(JSON.parse(spans.specText)).toEqual({
      spans: [{ ticker: 'OLD.US', start_date: '2020-01-02' }],
    });

    const rule = formFromUniverse(
      {
        id: 'cheap',
        kind: 'rule',
        spec: {
          start: '2025-01-02',
          rebalance: 'weekly',
          filters: [{ metric: 'dividend_yield', min: 0.04, max: null }],
          limit: 20,
        },
      },
      METRICS,
    );
    expect(rule.source).toBe('fields');
    expect(rule.fields.rebalance).toBe('weekly');
    expect(rule.fields.screen.filters[0]).toMatchObject({ metric: 'dividend_yield', min: '4' });
    expect(universeUpdateBody(rule, METRICS).spec).toEqual({
      rebalance: 'weekly',
      start: '2025-01-02',
      end: null,
      filters: [{ metric: 'dividend_yield', min: 0.04, max: null }],
      limit: 20,
    });

    const exchange = formFromUniverse({
      id: 'us',
      kind: 'exchange',
      spec: { exchange: 'US', include_delisted: false },
    });
    expect(exchange.fields).toMatchObject({ exchange: 'US', includeDelisted: false });
  });
});

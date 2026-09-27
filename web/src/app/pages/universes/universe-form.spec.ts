import {
  DEFAULT_FIELDS,
  type UniverseForm,
  parseSpec,
  specFromFields,
  specTemplate,
  universeCreateBody,
  universeFormErrors,
} from './universe-form';

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
    expect(
      specFromFields('rule', { ...f, minPrice: '', assetClasses: ['equity', 'crypto'] }),
    ).toEqual({
      rebalance: 'monthly',
      start: '2020-01-01',
      end: null,
      min_adv: 1000000,
      asset_classes: ['equity', 'crypto'],
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
    expect(
      universeFormErrors(form({ kind: 'rule', fields: { ...DEFAULT_FIELDS, minAdv: 'lots' } }))
        .minAdv,
    ).toBeDefined();
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
});

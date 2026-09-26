import {
  type UniverseForm,
  parseSpec,
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
    source: 'spec',
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

  it('parses a spec object and explains bad ones', () => {
    expect(parseSpec('{"a": 1}')).toEqual({ spec: { a: 1 } });
    expect(parseSpec('')).toEqual({ spec: {} });
    expect(parseSpec('[1]')).toEqual({ error: expect.stringContaining('JSON object') });
    expect(parseSpec('{nope')).toEqual({ error: 'The spec is not valid JSON.' });
  });

  it('checks the id, the spec and the CSV', () => {
    expect(universeFormErrors(form())).toEqual({});
    expect(universeFormErrors(form({ id: '' })).id).toBe('Enter an id.');
    expect(universeFormErrors(form({ id: 'has space' })).id).toContain('letters');
    expect(universeFormErrors(form({ specText: '{' })).spec).toBeDefined();
    expect(universeFormErrors(form({ source: 'csv' })).csv).toBe('Choose a CSV file.');
  });

  it('sends a spec, or a CSV with an empty spec', () => {
    expect(universeCreateBody(form({ name: ' Big ' }))).toEqual({
      id: 'big-caps',
      kind: 'list',
      name: 'Big',
      description: null,
      spec: { tickers: ['AAPL.US'] },
      csv: null,
    });
    expect(universeCreateBody(form({ source: 'csv', csv: 'ticker\nAAPL.US' }))).toMatchObject({
      spec: {},
      csv: 'ticker\nAAPL.US',
    });
  });
});

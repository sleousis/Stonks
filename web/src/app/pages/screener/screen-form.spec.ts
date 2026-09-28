import {
  emptyForm,
  filterRow,
  formatMetric,
  fromSpec,
  toSpec,
  UNIVERSE_ID,
  universeSlug,
} from './screen-form';
import { METRICS } from '../../../testing/screener-fixtures';

describe('screen form', () => {
  it('sends nothing for an empty form but the default top 50', () => {
    expect(toSpec(emptyForm(), METRICS)).toEqual({ spec: { limit: 50 }, errors: [] });
  });

  it('turns the form into a spec, percents as fractions', () => {
    const form = {
      ...emptyForm(),
      universeId: 'sp500',
      assetClasses: ['equity' as const],
      sectors: 'Technology, Health Care',
      excludeSectors: 'Utilities',
      exchanges: 'us, lse',
      minPrice: '5',
      minAdv: '1000000',
      filters: [filterRow('pe_ratio', '', '15'), filterRow('return_12m', '8', '')],
      sortBy: 'dividend_yield',
      descending: false,
      limit: '20',
      columns: ['price'],
    };
    const { spec, errors } = toSpec(form, METRICS);
    expect(errors).toEqual([]);
    expect(spec).toEqual({
      universe_id: 'sp500',
      asset_classes: ['equity'],
      sectors: ['Technology', 'Health Care'],
      exclude_sectors: ['Utilities'],
      exchanges: ['US', 'LSE'],
      min_price: 5,
      min_adv: 1_000_000,
      filters: [
        { metric: 'pe_ratio', min: null, max: 15 },
        { metric: 'return_12m', min: 0.08, max: null },
      ],
      sort_by: 'dividend_yield',
      descending: false,
      limit: 20,
      columns: ['price'],
    });
  });

  it('says what is wrong in words', () => {
    const form = {
      ...emptyForm(),
      minPrice: '-1',
      filters: [
        filterRow(),
        filterRow('pe_ratio'),
        filterRow('return_12m', '20', '10'),
        filterRow('price', 'abc'),
      ],
      limit: '0',
    };
    expect(toSpec(form, METRICS).errors).toEqual([
      'Lowest price must be a number, zero or more.',
      'Pick a metric for each filter, or remove the empty one.',
      'P/E: give a minimum, a maximum or both.',
      '12-month return: the minimum is above the maximum.',
      'Price: the bounds must be numbers.',
      'Show between 1 and 5000 rows, or leave it empty for all.',
    ]);
  });

  it('reads a saved spec back, percents in percent, without float noise', () => {
    const form = fromSpec(
      {
        universe_id: 'sp500',
        sectors: ['Energy'],
        filters: [{ metric: 'dividend_yield', min: 0.07, max: null }],
        sort_by: 'pe_ratio',
        descending: true,
        limit: 10,
      },
      METRICS,
    );
    expect(form.universeId).toBe('sp500');
    expect(form.sectors).toBe('Energy');
    expect(form.filters.map((f) => [f.metric, f.min, f.max])).toEqual([
      ['dividend_yield', '7', ''],
    ]);
    expect(form.limit).toBe('10');
    // Round trip: the same spec comes back.
    expect(toSpec(form, METRICS).spec.filters).toEqual([
      { metric: 'dividend_yield', min: 0.07, max: null },
    ]);
  });

  it('formats each unit', () => {
    expect(formatMetric(0.1234, 'percent')).toBe('12.3%');
    expect(formatMetric(2_500_000_000, 'money')).toBe('2.5B');
    expect(formatMetric(14.567, 'ratio')).toBe('14.57');
    expect(formatMetric(null, 'ratio')).toBe('–');
  });

  it('makes a valid universe id from a name', () => {
    expect(universeSlug('Cheap dividend payers!')).toBe('cheap-dividend-payers');
    expect(universeSlug('***')).toBe('my-screen');
    expect(UNIVERSE_ID.test(universeSlug('Café growth 2026'))).toBe(true);
  });
});

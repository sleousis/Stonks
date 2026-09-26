import { SURVIVAL_TEST_CATALOG } from './lab-test-fixtures';
import {
  buildTestOptions,
  filledCount,
  optionCatalog,
  optionFields,
  optionLabel,
  parseOption,
  testOptionErrors,
} from './test-options';

describe('test options from the API schema', () => {
  const catalog = optionCatalog(SURVIVAL_TEST_CATALOG);

  it('turns each property into a field with a label, default and bounds', () => {
    const [mode, psr, trades] = catalog['oos'];
    expect(mode).toMatchObject({ key: 'mode', label: 'Mode', kind: 'choice', defaultText: 'PSR' });
    expect(mode.choices?.map((c) => c.label)).toEqual(['PSR', 'Sharpe']);
    expect(psr).toMatchObject({
      label: 'Min PSR',
      kind: 'number',
      defaultText: '0.95',
      min: 0,
      max: 1,
      exclusiveMin: true,
      exclusiveMax: true,
    });
    expect(psr.hint).toBe('Above 0 and below 1.');
    expect(trades).toMatchObject({ kind: 'int', min: 0, defaultText: '20' });
  });

  it('reads booleans, lists, mixed choices and nullable numbers', () => {
    expect(catalog['deflated_sharpe'][1]).toMatchObject({ kind: 'choice', defaultText: 'Yes' });
    expect(catalog['cost_stress'][0]).toMatchObject({
      kind: 'list-number',
      defaultText: '0, 1, 2, 3',
    });
    const [retune, seed] = catalog['mcpt'];
    expect(retune.choices?.map((c) => c.label)).toEqual(['Yes', 'No', 'Auto']);
    expect(seed).toMatchObject({ kind: 'int', defaultText: '17' });
  });

  it('leaves out tests with no options and tests asked to skip', () => {
    expect(catalog['walk_forward']).toBeUndefined();
    expect(optionCatalog(SURVIVAL_TEST_CATALOG, ['mcpt'])['mcpt']).toBeUndefined();
  });

  it('labels acronyms and counts plainly', () => {
    expect(optionLabel('n_permutations', 'N Permutations')).toBe('Number of permutations');
    expect(optionLabel('min_return_to_dd', undefined)).toBe('Min return to DD');
  });

  it('checks values against the bounds and says the range', () => {
    const [, psr, trades] = catalog['oos'];
    expect(parseOption(psr, '1')).toEqual({ error: 'Must be above 0 and below 1.' });
    expect(parseOption(psr, '0.9')).toEqual({ value: 0.9 });
    expect(parseOption(trades, '2.5')).toEqual({ error: 'Enter a whole number.' });
    expect(parseOption(trades, '')).toEqual({});
    expect(parseOption(catalog['cost_stress'][0], '1, 2 4')).toEqual({ value: [1, 2, 4] });
    expect(parseOption(catalog['cost_stress'][0], '1, x')).toEqual({
      error: 'Enter numbers separated by commas.',
    });
  });

  it('sends only filled fields of tests in the suite', () => {
    const values = {
      oos: { min_psr: '0.9', mode: '' },
      deflated_sharpe: { include_prior_runs: 'false' },
      mcpt: { retune: '"auto"' },
    };
    expect(buildTestOptions(['oos', 'deflated_sharpe'], values, catalog)).toEqual({
      oos: { min_psr: 0.9 },
      deflated_sharpe: { include_prior_runs: false },
    });
    expect(buildTestOptions(['mcpt'], values, catalog)).toEqual({ mcpt: { retune: 'auto' } });
    expect(buildTestOptions(['oos'], {}, catalog)).toBeNull();
    expect(filledCount(values, 'oos')).toBe(1);
  });

  it('reports errors keyed by test and field', () => {
    const errors = testOptionErrors(
      ['deflated_sharpe'],
      { deflated_sharpe: { min_dsr: '0.5' } },
      catalog,
    );
    expect(errors).toEqual({ 'deflated_sharpe.min_dsr': 'Must be at least 0.8 and at most 0.99.' });
  });

  it('ignores properties it cannot render', () => {
    expect(optionFields({ properties: { nested: { type: 'object' } } })).toEqual([]);
  });
});

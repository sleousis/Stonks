import { SPEC } from '../../../../testing/lab-fixtures';
import {
  defaultParamValues,
  humanize,
  paramErrors,
  paramFields,
  paramPayload,
  rangeText,
} from './param-spec';

describe('param spec', () => {
  it('maps each kind to a control with bounds and defaults', () => {
    const [lookback, threshold, mode, longOnly, ticker] = paramFields(SPEC);
    expect(lookback).toMatchObject({
      control: 'int',
      label: 'Lookback days',
      min: 5,
      max: 250,
      step: 1,
      defaultValue: 20,
    });
    expect(threshold).toMatchObject({ control: 'float', min: 0, max: 0.2, step: 'any' });
    expect(mode).toMatchObject({
      control: 'choice',
      choices: ['fast', 'slow'],
      defaultValue: 'fast',
    });
    expect(longOnly).toMatchObject({ control: 'bool', defaultValue: true, tunable: false });
    expect(ticker).toMatchObject({ control: 'text', defaultValue: 'SPY.US' });
  });

  it('falls back to text for unknown kinds and open numeric bounds', () => {
    const [odd, open] = paramFields([
      { name: 'x', kind: 'weird', default: 3, tunable: false, description: '' },
      { name: 'n', kind: 'int', default: 1, bounds: null, tunable: true, description: '' },
    ]);
    expect(odd).toMatchObject({ control: 'text', defaultValue: '3' });
    expect(open).toMatchObject({ control: 'int', min: null, max: null });
    expect(rangeText(open)).toBeNull();
  });

  it('starts from the defaults', () => {
    expect(defaultParamValues(SPEC)).toEqual({
      lookback_days: 20,
      threshold: 0.01,
      mode: 'fast',
      long_only: true,
      ticker: 'SPY.US',
    });
  });

  it('reports bounds, integer, choice and blank errors', () => {
    const values = {
      ...defaultParamValues(SPEC),
      lookback_days: 2.5,
      threshold: 0.5,
      mode: 'medium',
      ticker: '  ',
    };
    expect(paramErrors(SPEC, values)).toEqual({
      lookback_days: 'Enter a whole number.',
      threshold: 'At most 0.2.',
      mode: 'Pick one of the choices.',
      ticker: 'Enter a value.',
    });
    expect(paramErrors(SPEC, { ...values, lookback_days: 1 })['lookback_days']).toBe('At least 5.');
    expect(paramErrors(SPEC, { ...values, lookback_days: null })['lookback_days']).toBe(
      'Enter a number.',
    );
    expect(paramErrors(SPEC, defaultParamValues(SPEC))).toEqual({});
  });

  it('builds a payload of known names with trimmed strings', () => {
    const payload = paramPayload(SPEC, { lookback_days: 30, ticker: ' QQQ.US ', extra: 1 });
    expect(payload).toEqual({
      lookback_days: 30,
      threshold: 0.01,
      mode: 'fast',
      long_only: true,
      ticker: 'QQQ.US',
    });
  });

  it('humanizes names and ranges', () => {
    expect(humanize('fast_ma-window')).toBe('Fast ma window');
    expect(rangeText(paramFields(SPEC)[0])).toBe('5 – 250');
  });
});

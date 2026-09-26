import { MOMENTUM } from '../../../testing/lab-fixtures';
import { presetFromStrategy, presetParamValues } from './strategy-preset';

describe('strategy presets', () => {
  it('keeps the registered id, class and params', () => {
    expect(
      presetFromStrategy({ id: 'm1', class_path: MOMENTUM.class_path, params: { a: 1 } }),
    ).toEqual({ strategyId: 'm1', classPath: MOMENTUM.class_path, params: { a: 1 } });
  });

  it('lays registered values over the class defaults', () => {
    const values = presetParamValues(MOMENTUM, {
      lookback_days: 60,
      mode: 'slow',
      unknown: 5,
      threshold: { nested: true },
    });
    expect(values).toEqual({
      lookback_days: 60,
      threshold: 0.01,
      mode: 'slow',
      long_only: true,
      ticker: 'SPY.US',
    });
  });
});

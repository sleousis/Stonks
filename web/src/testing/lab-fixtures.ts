import type { ParameterInfo } from '../app/api/models';

/** A parameter space with one field of every kind. */
export const SPEC: ParameterInfo[] = [
  {
    name: 'lookback_days',
    kind: 'int',
    default: 20,
    bounds: [5, 250],
    tunable: true,
    description: 'Momentum window.',
  },
  {
    name: 'threshold',
    kind: 'float',
    default: 0.01,
    bounds: [0, 0.2],
    tunable: true,
    description: '',
  },
  {
    name: 'mode',
    kind: 'categorical',
    default: 'fast',
    bounds: ['fast', 'slow'],
    tunable: true,
    description: 'Signal speed.',
  },
  { name: 'long_only', kind: 'bool', default: true, bounds: null, tunable: false, description: '' },
  {
    name: 'ticker',
    kind: 'categorical',
    default: 'SPY.US',
    bounds: null,
    tunable: false,
    description: '',
  },
];

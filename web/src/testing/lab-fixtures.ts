import type {
  BacktestResult,
  LabRunView,
  ParameterInfo,
  StrategyClassInfo,
} from '../app/api/models';

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

export const MOMENTUM: StrategyClassInfo = {
  class_path: 'stonks.strategies.examples.momentum:Momentum',
  name: 'momentum',
  source: 'builtin',
  description: 'Buys the strongest trailing returns.',
  applicable_asset_classes: ['equity'],
  parameters: SPEC,
};

export const BUY_AND_HOLD: StrategyClassInfo = {
  class_path: 'stonks.strategies.examples.buy_and_hold:BuyAndHold',
  name: 'buy_and_hold',
  source: 'builtin',
  description: 'Buys once and holds.',
  applicable_asset_classes: ['equity', 'crypto'],
  parameters: [],
};

export const MACRO: StrategyClassInfo = {
  class_path: 'stonks.strategies.macro_regime:MacroRegime',
  name: 'macro_regime',
  source: 'builtin',
  description: 'Risk on or off from macro indicators.',
  applicable_asset_classes: ['equity'],
  parameters: [],
};

export const CATALOG: StrategyClassInfo[] = [MOMENTUM, MACRO, BUY_AND_HOLD];

export const BACKTEST_RESULT: BacktestResult = {
  strategy_id: 'momentum',
  interval: '1d',
  start: '2026-01-02',
  end: '2026-01-07',
  final_return: 0.042,
  cagr: 0.18,
  sharpe: 1.35,
  max_drawdown: -0.05,
  profit_factor: 1.8,
  equity: [
    { timestamp: '2026-01-02T00:00:00', value: 10_000 },
    { timestamp: '2026-01-05T00:00:00', value: 10_500 },
    { timestamp: '2026-01-06T00:00:00', value: 9_975 },
    { timestamp: '2026-01-07T00:00:00', value: 10_420 },
  ],
};

export const LAB_RUN_VIEW: LabRunView = {
  class_path: MOMENTUM.class_path,
  best_params: { lookback_days: 60, mode: 'slow', long_only: true },
  best_score: 1.12,
  verdict: 'fail',
  registered_strategy_id: null,
  survival_reports: [
    {
      test_id: 'oos',
      passed: true,
      metrics: { is_score: 1.12, oos_score: 0.84 },
      notes: 'OOS held up.',
    },
    {
      test_id: 'walk_forward',
      passed: false,
      metrics: { mean_score: -0.1, positive_share: 0.25, n_splits: 4 },
      notes: '1 of 4 folds positive.',
    },
    {
      test_id: 'permutation',
      passed: true,
      metrics: { p_value: 0.02, real: 1.8, nan_metric: null },
      notes: '',
    },
  ],
};

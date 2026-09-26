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

/** A Phase 9 backtest: trade stats, risk, drawdown and a benchmark, with non-finite values as null. */
export const BACKTEST_RESULT_FULL: BacktestResult = {
  ...BACKTEST_RESULT,
  trade_count: 12,
  sortino: 2.1,
  calmar: null,
  ulcer_index: 0.0123,
  max_dd_duration_bars: 3,
  var_95: -0.021,
  es_95: null,
  drawdown: [
    { timestamp: '2026-01-02T00:00:00', value: 0 },
    { timestamp: '2026-01-05T00:00:00', value: 0 },
    { timestamp: '2026-01-06T00:00:00', value: -0.05 },
    { timestamp: '2026-01-07T00:00:00', value: -0.0076 },
  ],
  trade_stats: {
    n_trades: 12,
    n_open: 1,
    win_rate: 0.5833,
    avg_win: 120,
    avg_loss: -80,
    expectancy: 36.67,
    payoff_ratio: null,
    trade_profit_factor: 2.1,
    avg_bars_held: 4.5,
    exposure: 0.8,
    turnover_annual: 6.2,
    costs_paid: 42,
    cost_drag_annual: 0.0031,
  },
  benchmark: {
    name: 'SPY.US',
    spec: 'SPY.US',
    n_obs: 4,
    alpha_annual: 0.052,
    alpha_tstat: 1.3,
    beta: 0.8,
    correlation: 0.7,
    r2: 0.49,
    information_ratio: null,
    tracking_error: 0.11,
    excess_cagr: 0.031,
    benchmark_cagr: 0.149,
    benchmark_sharpe: 1.1,
    benchmark_max_dd: -0.04,
    residual_sharpe: 0.4,
    up_capture: 1.1,
    down_capture: 0.7,
  },
  benchmark_equity: [
    { timestamp: '2026-01-02T00:00:00', value: 10_000 },
    { timestamp: '2026-01-05T00:00:00', value: 10_100 },
    { timestamp: '2026-01-06T00:00:00', value: 10_050 },
    { timestamp: '2026-01-07T00:00:00', value: 10_200 },
  ],
};

/** A Phase 9 lab run: trial counts, a benchmark and the overfitting tests. */
export const LAB_RUN_VIEW_FULL: LabRunView = {
  ...LAB_RUN_VIEW,
  n_trials_run: 40,
  n_trials_class: 180,
  run_id: 'run_01',
  benchmark: BACKTEST_RESULT_FULL.benchmark,
  survival_reports: [
    {
      test_id: 'deflated_sharpe',
      passed: true,
      metrics: { dsr: 0.971, n_trials: 180, n_eff: 23.4, psr0: 0.99, var_sr: 0.01 },
      notes: '',
    },
    {
      test_id: 'pbo',
      passed: false,
      metrics: { pbo: null, p_loss: 0.4, degradation_slope: -0.3, n_trials: 40 },
      notes: '6 usable trials < 8',
    },
    {
      test_id: 'mc_trades',
      passed: true,
      metrics: {
        p95_max_dd: 0.18,
        median_max_dd: 0.09,
        risk_of_ruin: 0.01,
        prob_profit: 0.91,
        p05_return: -0.02,
        return_to_dd: 2.4,
      },
      notes: '',
    },
    {
      test_id: 'cost_stress',
      passed: true,
      metrics: {
        break_even_multiple: 4.5,
        sharpe_1x: 1.2,
        sharpe_2x: 0.9,
        cost_sr: 0.05,
        cost_drag_annual: 0.004,
        used_realistic_costs: 1,
      },
      notes: '',
    },
    {
      test_id: 'walk_forward',
      passed: true,
      metrics: { wfe: 0.72, sharpe_stitched: 1.05, positive_share: 0.75, n_folds: 4 },
      notes: '',
    },
  ],
};

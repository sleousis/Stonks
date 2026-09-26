import type { SignalIcView, SurvivalTestInfo, SweepResultView } from '../../api/models';

/**
 * Test data for the Lab specs (not used by the app).
 */

/** Trimmed copies of what `GET /api/lab/survival-tests` returns. */
export const SURVIVAL_TEST_CATALOG: SurvivalTestInfo[] = [
  {
    id: 'oos',
    description: 'Held-out data.',
    presets: ['quick', 'standard', 'promotion'],
    options_schema: {
      type: 'object',
      properties: {
        mode: { default: 'psr', enum: ['psr', 'sharpe'], title: 'Mode', type: 'string' },
        min_psr: {
          default: 0.95,
          exclusiveMaximum: 1,
          exclusiveMinimum: 0,
          title: 'Min Psr',
          type: 'number',
        },
        min_trades: { default: 20, minimum: 0, title: 'Min Trades', type: 'integer' },
      },
    },
  },
  {
    id: 'deflated_sharpe',
    description: 'Trials.',
    presets: ['standard', 'promotion'],
    options_schema: {
      type: 'object',
      properties: {
        min_dsr: { default: 0.95, maximum: 0.99, minimum: 0.8, title: 'Min Dsr', type: 'number' },
        include_prior_runs: { default: true, title: 'Include Prior Runs', type: 'boolean' },
      },
    },
  },
  {
    id: 'cost_stress',
    description: 'Costs.',
    presets: ['standard', 'promotion'],
    options_schema: {
      type: 'object',
      properties: {
        multipliers: {
          default: [0, 1, 2, 3],
          items: { type: 'number' },
          title: 'Multipliers',
          type: 'array',
        },
      },
    },
  },
  {
    id: 'mcpt',
    description: 'Shuffles.',
    presets: ['promotion'],
    options_schema: {
      type: 'object',
      properties: {
        retune: {
          anyOf: [{ type: 'boolean' }, { const: 'auto', type: 'string' }],
          default: false,
          title: 'Retune',
        },
        seed: { anyOf: [{ type: 'integer' }, { type: 'null' }], default: 17, title: 'Seed' },
      },
    },
  },
  {
    id: 'walk_forward',
    description: 'Folds.',
    presets: ['standard'],
    options_schema: { type: 'object', properties: {} },
  },
];

/** A sweep of three strategies: one passed, one failed, one errored. */
export const SWEEP_RESULT: SweepResultView = {
  universe: ['SPY.US', 'QQQ.US'],
  passed: 1,
  failed: 1,
  errors: 1,
  rows: [
    {
      strategy: 'buy_and_hold',
      ticker: null,
      verdict: 'fail',
      best_score: 1.4,
      n_trials: 1,
      survival: { oos: { passed: false }, period_stability: { passed: true } },
    },
    {
      strategy: 'macro_regime',
      ticker: null,
      verdict: 'error',
      best_score: null,
      n_trials: 0,
      error: 'needs an inner strategy',
    },
    {
      strategy: 'momentum',
      ticker: null,
      verdict: 'pass',
      best_score: 0.9,
      n_trials: 20,
      survival: { oos: { passed: true }, period_stability: { passed: true } },
    },
  ],
};

/** Signal IC at three horizons; the longest one has too few dates for a t-stat. */
export const SIGNAL_IC_VIEW: SignalIcView = {
  strategy_id: 'momentum',
  window: ['2025-09-26', '2026-09-26'],
  n_tickers: 12,
  n_dates: 48,
  every_bars: 5,
  n_quantiles: 3,
  status: 'ok',
  ic_horizon: 5,
  ic_estimate: 0.041,
  score_turnover: 0.2,
  horizons: [
    {
      horizon: 21,
      n_dates: 40,
      mean_ic: 0.02,
      ic_std: 0.1,
      icir: 0.2,
      hit_rate: 0.55,
      se_iid: 0.01,
      se_hac: 0.02,
      hac_lags: 4,
      t_stat_hac: null,
      quantile_means: [-0.01, 0.0, 0.012],
      spread_mean: 0.022,
      spread_t_hac: 1.1,
    },
    {
      horizon: 1,
      n_dates: 48,
      mean_ic: 0.05,
      ic_std: 0.1,
      icir: 0.5,
      hit_rate: 0.6,
      se_iid: 0.01,
      se_hac: 0.02,
      hac_lags: 1,
      t_stat_hac: 2.5,
      quantile_means: [-0.002, 0.001, 0.003],
      spread_mean: 0.005,
      spread_t_hac: 2.1,
    },
  ],
};

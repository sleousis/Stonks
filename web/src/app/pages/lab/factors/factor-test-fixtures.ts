import type { FactorCatalogView, FactorTearSheetView, FactorView } from '../../../api/models';

/** Test data for the factor pages' specs. */

export const MOM: FactorView = {
  id: 'mom_12_1',
  set: 'classic',
  family: 'momentum',
  kind: 'expression',
  direction: 1,
  description: 'Twelve-month return, skipping the last month.',
  hypothesis: 'Winners keep winning for months because investors under-react to news.',
  expression: 'Ref($close, 21) / Ref($close, 252) - 1',
  lookback_bars: 252,
  asset_classes: ['equity', 'crypto'],
};

export const LOW_VOL: FactorView = {
  id: 'low_vol_60',
  set: 'classic',
  family: 'volatility',
  kind: 'expression',
  direction: -1,
  description: 'Sixty-day volatility of daily returns.',
  hypothesis: 'Quiet stocks earn more than their risk suggests.',
  expression: 'Std($close / Ref($close, 1) - 1, 60)',
  lookback_bars: 61,
  asset_classes: ['equity'],
};

export const PIOTROSKI: FactorView = {
  id: 'piotroski_f',
  set: 'fundamentals',
  family: 'quality',
  kind: 'fundamental',
  direction: 1,
  description: 'Piotroski F-score, nine accounting checks.',
  hypothesis: 'Firms with improving fundamentals beat those with worsening ones.',
  expression: null,
  lookback_bars: 0,
  asset_classes: ['equity'],
};

export const FACTOR_CATALOG: FactorCatalogView = {
  factors: [MOM, LOW_VOL, PIOTROSKI],
  families: ['momentum', 'quality', 'volatility'],
  sets: [
    { name: 'classic', count: 2 },
    { name: 'fundamentals', count: 1 },
  ],
};

export const TEARSHEET: FactorTearSheetView = {
  factor: MOM,
  status: 'ok',
  window: ['2023-01-02', '2026-01-02'],
  interval: '1d',
  every_bars: 5,
  n_quantiles: 3,
  n_tickers: 40,
  n_dates: 150,
  coverage: 0.95,
  universe_id: 'sp40',
  ic_horizon: 21,
  horizons: [
    {
      horizon: 21,
      mean_ic: 0.042,
      ic_std: 0.1,
      icir: 0.42,
      hit_rate: 0.61,
      t_stat_hac: 2.3,
      hac_lags: 4,
      n_dates: 150,
      quantile_means: [-0.004, 0.003, 0.012],
      spread_mean: 0.016,
      spread_t_hac: 2.1,
    },
    {
      horizon: 1,
      mean_ic: null,
      ic_std: null,
      icir: null,
      hit_rate: null,
      t_stat_hac: null,
      hac_lags: 0,
      n_dates: 0,
      quantile_means: [null, null, null],
      spread_mean: null,
      spread_t_hac: null,
    },
  ],
  ic_by_group: {
    sector: [
      { group: 'Technology', mean_ic: 0.05, t_stat_hac: 1.9, n_dates: 150, mean_names: 12 },
      { group: 'Energy', mean_ic: -0.01, t_stat_hac: null, n_dates: 150, mean_names: 4 },
    ],
    asset_class: [],
  },
  quantile_curves: {
    dates: ['2023-01-09', '2023-01-16', '2023-01-23'],
    series: [
      [-0.01, -0.02, -0.015],
      [0.0, 0.004, 0.006],
      [0.01, 0.025, 0.03],
    ],
    spread: [0.02, 0.046, 0.046],
  },
  monthly_ic: [{ year: 2024, months: [0.05, -0.02, null, 0, 0, 0, 0, 0, 0, 0, 0, 0.01] }],
  alpha_beta: {
    alpha_annual: 0.061,
    alpha_t: 2.4,
    beta: 0.12,
    r_squared: 0.03,
    n_periods: 150,
    benchmark: 'equal-weight universe',
  },
  score_turnover: 0.18,
  top_quantile_turnover: 0.25,
  size_basis: 'dollar_volume',
};

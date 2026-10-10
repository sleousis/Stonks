import { formatNumber, formatPercent } from '../core/format/format';

/**
 * Formatting and labels for the metric maps the API returns (survival
 * reports, benchmark stats). The API sends non-finite values as `null`;
 * they show as "n/a" so a trader can tell "not computable" from zero.
 */
export const NA = 'n/a';

/**
 * Keys shown as percentages (the API sends fractions), also with a held-out,
 * in-sample or stitched suffix (`max_drawdown_oos`, `cagr_oos_stitched`).
 */
const PERCENT_KEY =
  /(return|returns|drawdown|_dd|share|cagr|cagr_oos|cagr_mean|ruin|prob_profit|cost_drag_annual|alpha_annual|tracking_error|var_95|es_95|exposure|win_rate|ratio_pct|fee_rate)(_oos|_is|_stitched)?$/;
const MULTIPLE_KEY =
  /(break_even_multiple|stress_multiplier|capture|return_to_dd|turnover_annual)$/;
const FLAG_KEY = /^(used_realistic_costs)$/;

export function formatMetric(key: string, value: number | null | undefined): string {
  if (value === null || value === undefined || !Number.isFinite(value)) return NA;
  if (FLAG_KEY.test(key)) return value ? 'Yes' : 'No';
  if (MULTIPLE_KEY.test(key)) return `${formatNumber(value, { digits: 2 })}×`;
  if (PERCENT_KEY.test(key)) return formatPercent(value);
  if (/p_value$/.test(key)) return formatNumber(value, { digits: 3 });
  return formatNumber(value, { digits: Number.isInteger(value) ? 0 : 3 });
}

const LABELS: Record<string, string> = {
  is_score: 'Past score',
  oos_score: 'Score on unseen data',
  p_value: 'Chance it is luck',
  n_splits: 'Retest periods',
  n_folds: 'Retest periods',
  n_bars: 'Data points tested',
  sharpe_oos: 'Sharpe on unseen data',
  cagr_oos: 'Yearly return on unseen data',
  max_drawdown_oos: 'Worst drop on unseen data',
  final_return_oos: 'Return on unseen data',
  psr: 'Probabilistic Sharpe vs target',
  psr0: 'Probabilistic Sharpe',
  psr0_stitched: 'Probabilistic Sharpe across retests',
  dsr: 'Deflated Sharpe',
  skew: 'Skew of returns',
  kurtosis: 'Fat tails of returns',
  rho: 'Day to day carry-over',
  var_sr: 'Sharpe variance',
  sr0_per_bar: 'Luck threshold per bar',
  n_trials: 'Settings tried',
  n_trials_run: 'Settings tried this run',
  n_trials_usable: 'Usable settings',
  n_eff: 'Independent settings',
  n_trades: 'Trades',
  pbo: 'Chance the best setting was luck',
  spa_p: 'SPA p-value',
  spa_p_lower: 'SPA p-value (lower)',
  spa_p_upper: 'SPA p-value (upper)',
  reality_check_p: 'Reality Check p-value',
  romano_wolf_rejected: 'Settings that beat cash',
  selected_rejected: 'Chosen setting beats cash',
  selected_p: 'Chosen setting adjusted p-value',
  n_family_runs: 'Runs in the family',
  degradation_slope: 'Drop from past to new data',
  p_loss: 'Chance of a loss on unseen data',
  risk_of_ruin: 'Risk of ruin',
  median_max_dd: 'Typical worst drop',
  p95_max_dd: 'Bad case worst drop',
  median_return: 'Typical return',
  p05_return: 'Bad case return',
  return_to_dd: 'Return to drawdown',
  prob_profit: 'Chance of profit',
  break_even_multiple: 'Costs it can bear',
  cost_sr: 'Sharpe lost to costs',
  cost_sr_limit: 'Most Sharpe costs may take',
  cost_drag_annual: 'Cost drag per year',
  turnover_annual: 'Turnover per year',
  used_realistic_costs: 'Realistic costs',
  borrow_fee_rate: 'Borrow fee per year',
  borrow_stress_multiplier: 'Borrow fee stress',
  sharpe_borrow_stress: 'Sharpe at higher borrow fees',
  financing_paid: 'Financing paid',
  sharpe_std: 'Spread of scores between periods',
  sharpe_min: 'Worst period score',
  sharpe_max: 'Best period score',
  max_sharpe_std: 'Most spread allowed',
  min_period_sharpe: 'Lowest period score allowed',
  n_windows: 'Periods checked',
  correlation_mean: 'Average match with noisy prices',
  correlation_min: 'Lowest match with noisy prices',
  level_correlation_min: 'Lowest value match with noisy prices',
  levels_tested: 'Noise levels tried',
  wfe: 'Share of past return kept',
  sharpe_stitched: 'Sharpe across all retests',
  cagr_oos_stitched: 'Yearly return across all retests',
  oos_score_mean: 'Average retest score',
  is_score_mean: 'Average past score',
  is_cagr_mean: 'Average past yearly return',
  positive_share: 'Retests that made money',
  matrix_pass_share: 'Retest lengths passed',
  matrix_cells: 'Retest lengths tried',
  matrix_cells_passed: 'Retest lengths that passed',
  n_bars_stitched: 'Data points across retests',
  excess_cagr: 'Yearly return above the benchmark',
  benchmark_cagr: 'Benchmark yearly return',
  benchmark_max_dd: 'Benchmark worst drop',
  information_ratio: 'Information ratio',
  alpha_annual: 'Alpha per year',
  alpha_tstat: 'Alpha t-stat',
  beta: 'Beta',
  up_capture: 'Up capture',
  down_capture: 'Down capture',
  tracking_error: 'Tracking error',
  sr_per_bar: 'Sharpe per bar',
  sharpe_se: 'Sharpe standard error',
  min_trl_bars: 'Shortest track record needed (bars)',
  sharpe_ci_low: 'Sharpe range, low',
  sharpe_ci_high: 'Sharpe range, high',
  trade_expectancy: 'Average result per trade',
  n_tickers: 'Tickers',
  n_dates: 'Dates checked',
  ic_estimate: 'Ranking skill',
  ic_horizon: 'Days ahead checked',
  score_turnover: 'Score turnover',
  top_quantile_turnover: 'Top group turnover',
  lot_orders: 'Orders',
  lot_rounded_orders: 'Orders rounded to whole shares',
  lot_skipped_orders: 'Orders too small to place',
  lot_skipped_share: 'Share of orders too small',
  lot_skipped_notional: 'Value of orders too small',
  lot_mean_drift: 'Average drift from rounding',
  lot_max_drift: 'Largest drift from rounding',
  min_capital: 'Smallest account that works',
};

/** Keys with a number in them: walk-forward folds, matrix cells, IC horizons. */
const PATTERN_LABELS: readonly [RegExp, (m: RegExpExecArray) => string][] = [
  [/^sharpe_(\d+(?:\.\d+)?)x$/, (m) => `Sharpe at ${m[1]}× costs`],
  [/^fold(\d+)_oos_score$/, (m) => `Retest ${+m[1] + 1} score`],
  [/^fold(\d+)_is_score$/, (m) => `Retest ${+m[1] + 1} past score`],
  [/^fold(\d+)_max_drawdown_oos$/, (m) => `Retest ${+m[1] + 1} worst drop`],
  [/^fold(\d+)_cagr_oos$/, (m) => `Retest ${+m[1] + 1} yearly return`],
  [/^fold(\d+)_cagr_is$/, (m) => `Retest ${+m[1] + 1} past yearly return`],
  [/^matrix_(\d+)x(\d+)_passed$/, (m) => `${m[1]} by ${m[2]} bar retest passed`],
  [/^matrix_(\d+)x(\d+)_wfe$/, (m) => `${m[1]} by ${m[2]} bar retest, past return kept`],
  [/^ic_mean_h(\d+)$/, (m) => `Ranking skill ${m[1]} days ahead`],
  [/^icir_h(\d+)$/, (m) => `Ranking steadiness ${m[1]} days ahead`],
  [/^ic_t_hac_h(\d+)$/, (m) => `Ranking t-stat ${m[1]} days ahead`],
  [/^spread_h(\d+)$/, (m) => `Top minus bottom ${m[1]} days ahead`],
];

export function humanizeKey(key: string): string {
  const words = key.replace(/_/g, ' ').trim();
  return words ? words.charAt(0).toUpperCase() + words.slice(1) : key;
}

export function metricLabel(key: string): string {
  if (LABELS[key]) return LABELS[key];
  for (const [pattern, label] of PATTERN_LABELS) {
    const m = pattern.exec(key);
    if (m) return label(m);
  }
  return humanizeKey(key);
}

/**
 * The few figures that decide each survival test, shown first; the rest
 * sit behind "All figures".
 */
export const KEY_METRICS: Record<string, readonly string[]> = {
  oos: ['sharpe_oos', 'psr0', 'cagr_oos', 'max_drawdown_oos', 'n_trades'],
  deflated_sharpe: ['dsr', 'n_trials', 'n_eff', 'psr0'],
  pbo: ['pbo', 'p_loss', 'degradation_slope', 'n_trials'],
  data_snooping: [
    'spa_p',
    'reality_check_p',
    'romano_wolf_rejected',
    'selected_rejected',
    'n_trials',
  ],
  mc_trades: ['p95_max_dd', 'median_max_dd', 'risk_of_ruin', 'prob_profit', 'p05_return'],
  cost_stress: ['break_even_multiple', 'sharpe_1x', 'sharpe_2x', 'cost_sr', 'cost_drag_annual'],
  walk_forward: ['wfe', 'sharpe_stitched', 'positive_share', 'n_folds', 'matrix_pass_share'],
  benchmark_relative: ['excess_cagr', 'information_ratio', 'beta', 'alpha_annual'],
  mcpt: ['p_value'],
  permutation: ['p_value'],
  walk_forward_mcpt: ['p_value'],
};

export interface MetricView {
  key: string;
  label: string;
  value: string;
}

/** Split a test's metrics into the key figures and the rest. */
export function splitMetrics(
  testId: string,
  metrics: Record<string, number | null>,
): { key: MetricView[]; rest: MetricView[] } {
  const view = (key: string): MetricView => ({
    key,
    label: metricLabel(key),
    value: formatMetric(key, metrics[key]),
  });
  const wanted = (KEY_METRICS[testId] ?? []).filter((k) => k in metrics);
  const keys = wanted.length ? wanted : Object.keys(metrics).slice(0, 4);
  return {
    key: keys.map(view),
    rest: Object.keys(metrics)
      .filter((k) => !keys.includes(k))
      .map(view),
  };
}

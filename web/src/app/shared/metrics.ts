import { formatNumber, formatPercent } from '../core/format/format';

/**
 * Formatting and labels for the metric maps the API returns (survival
 * reports, benchmark stats). The API sends non-finite values as `null`;
 * they show as "n/a" so a trader can tell "not computable" from zero.
 */
export const NA = 'n/a';

/** Keys shown as percentages (the API sends fractions). */
const PERCENT_KEY =
  /(return|returns|drawdown|_dd|share|cagr|cagr_oos|ruin|prob_profit|cost_drag_annual|alpha_annual|tracking_error|var_95|es_95|exposure|win_rate|ratio_pct)$/;
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
  is_score: 'In-sample score',
  oos_score: 'Out-of-sample score',
  p_value: 'p-value',
  n_splits: 'Folds',
  n_folds: 'Folds',
  sharpe_oos: 'Sharpe (held out)',
  cagr_oos: 'CAGR (held out)',
  max_drawdown_oos: 'Max drawdown (held out)',
  final_return_oos: 'Return (held out)',
  psr0: 'Probabilistic Sharpe',
  psr0_stitched: 'Probabilistic Sharpe (stitched)',
  dsr: 'Deflated Sharpe',
  n_trials: 'Trials',
  n_trials_run: 'Trials this run',
  n_trials_usable: 'Usable trials',
  n_eff: 'Independent trials',
  n_trades: 'Trades',
  pbo: 'PBO',
  spa_p: 'SPA p-value',
  spa_p_lower: 'SPA p-value (lower)',
  spa_p_upper: 'SPA p-value (upper)',
  reality_check_p: 'Reality Check p-value',
  romano_wolf_rejected: 'Trials that beat cash (Romano-Wolf)',
  selected_rejected: 'Chosen trial beats cash',
  selected_p: 'Chosen trial adjusted p-value',
  n_family_runs: 'Runs in the family',
  degradation_slope: 'Degradation slope',
  p_loss: 'Chance of a loss out of sample',
  risk_of_ruin: 'Risk of ruin',
  median_max_dd: 'Median drawdown (MC)',
  p95_max_dd: '95th percentile drawdown (MC)',
  median_return: 'Median return (MC)',
  p05_return: '5th percentile return (MC)',
  return_to_dd: 'Return to drawdown',
  prob_profit: 'Chance of profit',
  break_even_multiple: 'Break-even cost multiple',
  cost_sr: 'Sharpe lost to costs',
  cost_sr_limit: 'Cost Sharpe limit',
  cost_drag_annual: 'Cost drag per year',
  turnover_annual: 'Turnover per year',
  used_realistic_costs: 'Realistic costs',
  wfe: 'Walk-forward efficiency',
  sharpe_stitched: 'Sharpe (stitched out of sample)',
  cagr_oos_stitched: 'CAGR (stitched out of sample)',
  oos_score_mean: 'Mean out-of-sample score',
  is_score_mean: 'Mean in-sample score',
  is_cagr_mean: 'Mean in-sample CAGR',
  positive_share: 'Positive folds',
  matrix_pass_share: 'Matrix cells passed',
  matrix_cells: 'Matrix cells',
  matrix_cells_passed: 'Matrix cells that passed',
  n_bars_stitched: 'Stitched bars',
  excess_cagr: 'Excess CAGR',
  information_ratio: 'Information ratio',
  alpha_annual: 'Alpha per year',
  alpha_tstat: 'Alpha t-stat',
  beta: 'Beta',
  up_capture: 'Up capture',
  down_capture: 'Down capture',
  tracking_error: 'Tracking error',
  sr_per_bar: 'Sharpe per bar',
  sharpe_se: 'Sharpe standard error',
  min_trl_bars: 'Minimum track record (bars)',
  sharpe_ci_low: 'Sharpe interval, low',
  sharpe_ci_high: 'Sharpe interval, high',
  trade_expectancy: 'Expectancy per trade',
};

export function humanizeKey(key: string): string {
  const words = key.replace(/_/g, ' ').trim();
  return words ? words.charAt(0).toUpperCase() + words.slice(1) : key;
}

export function metricLabel(key: string): string {
  const stress = /^sharpe_(\d+(?:\.\d+)?)x$/.exec(key);
  if (stress) return `Sharpe at ${stress[1]}× costs`;
  return LABELS[key] ?? humanizeKey(key);
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

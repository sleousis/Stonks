/**
 * The "advanced test options" editor: the options each survival test
 * accepts (mirroring the test's `Options` model or constructor in
 * `src/stonks/lab/survival/`), validated in the browser so mistakes show
 * next to the field, and turned into the request's `test_options`.
 *
 * Every field starts blank, which means "the test's default"; only fields
 * the trader fills are sent. Walk-forward and the permutation test have
 * their own fields in the form, so they are not listed here.
 */

export type OptionKind = 'number' | 'int' | 'bool' | 'choice' | 'tickers';

export interface OptionField {
  key: string;
  label: string;
  kind: OptionKind;
  /** Shown as the placeholder: what blank means. */
  defaultText: string;
  hint?: string;
  min?: number;
  max?: number;
  /** Bounds are exclusive (strictly greater / less). */
  exclusiveMin?: boolean;
  exclusiveMax?: boolean;
  even?: boolean;
  choices?: readonly { value: string; label: string }[];
}

const WINDOW: OptionField = {
  key: 'window',
  label: 'Scoring window',
  kind: 'choice',
  defaultText: 'Held-out data',
  choices: [
    { value: 'val', label: 'Held-out data only' },
    { value: 'full', label: 'Whole window (includes tuning data)' },
  ],
  hint: 'Scoring the tuning data counts the fit as evidence; keep held-out unless you know why.',
};

export const TEST_OPTION_FIELDS: Readonly<Record<string, readonly OptionField[]>> = {
  oos: [
    {
      key: 'mode',
      label: 'Pass rule',
      kind: 'choice',
      defaultText: 'Probabilistic Sharpe',
      choices: [
        { value: 'psr', label: 'Probabilistic Sharpe' },
        { value: 'sharpe', label: 'Plain Sharpe' },
      ],
    },
    {
      key: 'min_psr',
      label: 'Min probabilistic Sharpe',
      kind: 'number',
      defaultText: '0.95',
      min: 0,
      max: 1,
      exclusiveMin: true,
      exclusiveMax: true,
    },
    { key: 'min_sharpe', label: 'Min Sharpe', kind: 'number', defaultText: '0.5' },
    {
      key: 'max_drawdown_limit',
      label: 'Worst drawdown allowed',
      kind: 'number',
      defaultText: '-0.3',
      max: 0,
      hint: 'A negative fraction: -0.3 is a 30% fall.',
    },
    { key: 'min_trades', label: 'Min trades', kind: 'int', defaultText: '20', min: 0 },
  ],
  period_stability: [
    { key: 'n_windows', label: 'Sub-periods', kind: 'int', defaultText: '3', min: 2 },
    {
      key: 'max_sharpe_std',
      label: 'Max Sharpe spread',
      kind: 'number',
      defaultText: '1.0',
      min: 0,
    },
    {
      key: 'min_period_sharpe',
      label: 'Min Sharpe in any period',
      kind: 'number',
      defaultText: '0',
    },
    WINDOW,
  ],
  perturbation: [
    {
      key: 'min_correlation',
      label: 'Min correlation with the original',
      kind: 'number',
      defaultText: '0.8',
      min: 0,
      max: 1,
    },
    WINDOW,
  ],
  drift: [
    {
      key: 'max_psi',
      label: 'Max PSI',
      kind: 'number',
      defaultText: '0.25',
      min: 0,
      exclusiveMin: true,
    },
    { key: 'sample_dates', label: 'Sample dates', kind: 'int', defaultText: '20', min: 1 },
    { key: 'bins', label: 'Bins', kind: 'int', defaultText: '5', min: 2 },
  ],
  runs_test: [
    {
      key: 'max_abs_z_score',
      label: 'Max |z| score',
      kind: 'number',
      defaultText: '3.0',
      min: 0,
      exclusiveMin: true,
    },
    { key: 'trade_level', label: 'Count trades, not bars', kind: 'bool', defaultText: 'No' },
    WINDOW,
  ],
  deflated_sharpe: [
    {
      key: 'min_dsr',
      label: 'Min deflated Sharpe',
      kind: 'number',
      defaultText: '0.95',
      min: 0.8,
      max: 0.99,
    },
    {
      key: 'include_prior_runs',
      label: 'Count earlier runs of this class',
      kind: 'bool',
      defaultText: 'Yes',
      hint: 'Every trial ever tried makes a lucky Sharpe likelier; leave on.',
    },
    {
      key: 'n_eff_method',
      label: 'Independent-trial estimate',
      kind: 'choice',
      defaultText: 'Effective rank',
      choices: [
        { value: 'effective_rank', label: 'Effective rank' },
        { value: 'avg_corr', label: 'Average correlation' },
        { value: 'raw', label: 'Raw trial count' },
      ],
    },
  ],
  pbo: [
    {
      key: 'n_blocks',
      label: 'Blocks',
      kind: 'int',
      defaultText: '10',
      min: 8,
      max: 16,
      even: true,
    },
    { key: 'max_pbo', label: 'Max PBO', kind: 'number', defaultText: '0.2', min: 0, max: 1 },
    { key: 'min_trials', label: 'Min usable trials', kind: 'int', defaultText: '8', min: 2 },
  ],
  mc_trades: [
    {
      key: 'n_paths',
      label: 'Paths',
      kind: 'int',
      defaultText: '5000',
      min: 1000,
      max: 20000,
    },
    {
      key: 'ruin_drawdown',
      label: 'Ruin drawdown',
      kind: 'number',
      defaultText: '0.40',
      min: 0,
      max: 1,
      exclusiveMin: true,
      exclusiveMax: true,
    },
    { key: 'min_trades', label: 'Min trades', kind: 'int', defaultText: '30', min: 1 },
    {
      key: 'max_risk_of_ruin',
      label: 'Max risk of ruin',
      kind: 'number',
      defaultText: '0.10',
      min: 0,
      max: 1,
    },
    {
      key: 'min_return_to_dd',
      label: 'Min return to drawdown',
      kind: 'number',
      defaultText: '2.0',
      min: 0.5,
      max: 4,
    },
    {
      key: 'min_prob_profit',
      label: 'Min chance of profit',
      kind: 'number',
      defaultText: '0.8',
      min: 0,
      max: 1,
    },
  ],
  cost_stress: [
    {
      key: 'stress_multiplier',
      label: 'Stress multiple',
      kind: 'number',
      defaultText: '2.0',
      min: 1,
      exclusiveMin: true,
    },
    {
      key: 'min_stressed_sharpe_fraction',
      label: 'Sharpe kept under stress',
      kind: 'number',
      defaultText: '0.5',
      min: 0,
      max: 1,
    },
    {
      key: 'min_break_even',
      label: 'Min break-even multiple',
      kind: 'number',
      defaultText: '2.0',
      min: 0,
    },
    {
      key: 'max_cost_sharpe',
      label: 'Max Sharpe lost to costs',
      kind: 'number',
      defaultText: '0.13',
      min: 0,
    },
  ],
  plateau: [
    { key: 'step', label: 'Step', kind: 'number', defaultText: '0.15', min: 0.05, max: 0.3 },
    {
      key: 'min_oos_sharpe_fraction',
      label: 'Sharpe kept by neighbours',
      kind: 'number',
      defaultText: '0.5',
      min: 0,
      max: 1,
    },
    { key: 'min_neighbours', label: 'Min neighbours', kind: 'int', defaultText: '2', min: 1 },
    {
      key: 'min_robustness_ratio',
      label: 'Min robustness ratio',
      kind: 'number',
      defaultText: 'Report only',
      min: 0,
      max: 1,
    },
  ],
  cross_instrument: [
    {
      key: 'min_positive_share',
      label: 'Share of tickers that must profit',
      kind: 'number',
      defaultText: '0.6',
      min: 0.5,
      max: 0.8,
    },
    {
      key: 'max_pnl_share',
      label: 'Max profit from one ticker',
      kind: 'number',
      defaultText: '0.5',
      min: 0,
      max: 1,
      exclusiveMin: true,
    },
    { key: 'min_tickers', label: 'Min tickers', kind: 'int', defaultText: '3', min: 2 },
    {
      key: 'held_out',
      label: 'Extra tickers to test',
      kind: 'tickers',
      defaultText: 'None',
      hint: 'Tickers outside the universe, e.g. QQQ.US IWM.US.',
    },
    {
      key: 'held_out_auto',
      label: 'More tickers from the lake',
      kind: 'int',
      defaultText: '0',
      min: 0,
    },
  ],
  benchmark_relative: [
    {
      key: 'min_ir',
      label: 'Min information ratio',
      kind: 'number',
      defaultText: '0',
    },
    {
      key: 'min_excess_cagr',
      label: 'Min excess CAGR',
      kind: 'number',
      defaultText: '0',
      hint: 'A fraction: 0.02 means beat the benchmark by 2% a year.',
    },
    {
      key: 'require_alpha_tstat',
      label: 'Min alpha t-stat',
      kind: 'number',
      defaultText: 'Not required',
    },
  ],
};

/** Raw text per test, per field, as typed. */
export type TestOptionValues = Readonly<Record<string, Readonly<Record<string, string>>>>;

function describeRange(f: OptionField): string {
  const lo = f.min === undefined ? null : `${f.exclusiveMin ? 'above' : 'at least'} ${f.min}`;
  const hi = f.max === undefined ? null : `${f.exclusiveMax ? 'below' : 'at most'} ${f.max}`;
  return [lo, hi].filter(Boolean).join(' and ');
}

/** The value to send, or an error message. Blank is `undefined` (use the default). */
export function parseOption(
  f: OptionField,
  raw: string | undefined,
): { value?: unknown; error?: string } {
  const text = (raw ?? '').trim();
  if (!text) return {};
  switch (f.kind) {
    case 'bool':
      return { value: text === 'true' };
    case 'choice':
      return f.choices?.some((c) => c.value === text)
        ? { value: text }
        : { error: 'Pick one of the listed options.' };
    case 'tickers': {
      const list = text
        .split(/[\s,;]+/)
        .map((t) => t.trim().toUpperCase())
        .filter(Boolean);
      return { value: [...new Set(list)] };
    }
    case 'int':
    case 'number': {
      const n = Number(text);
      if (!Number.isFinite(n)) return { error: 'Enter a number.' };
      if (f.kind === 'int' && !Number.isInteger(n)) return { error: 'Enter a whole number.' };
      const range = describeRange(f);
      const low = f.min !== undefined && (f.exclusiveMin ? n <= f.min : n < f.min);
      const high = f.max !== undefined && (f.exclusiveMax ? n >= f.max : n > f.max);
      if (low || high) return { error: `Must be ${range}.` };
      if (f.even && n % 2 !== 0) return { error: 'Must be an even number.' };
      return { value: n };
    }
  }
}

/** Field errors keyed `<test>.<field>`, for the tests in `suite` only. */
export function testOptionErrors(
  suite: readonly string[],
  values: TestOptionValues,
): Record<string, string> {
  const errors: Record<string, string> = {};
  for (const test of suite) {
    for (const f of TEST_OPTION_FIELDS[test] ?? []) {
      const { error } = parseOption(f, values[test]?.[f.key]);
      if (error) errors[`${test}.${f.key}`] = error;
    }
  }
  return errors;
}

/**
 * `test_options` for the request: only tests in the suite, only filled
 * fields. `null` when nothing is set (the request then omits it).
 */
export function buildTestOptions(
  suite: readonly string[],
  values: TestOptionValues,
): Record<string, Record<string, unknown>> | null {
  const out: Record<string, Record<string, unknown>> = {};
  for (const test of suite) {
    const opts: Record<string, unknown> = {};
    for (const f of TEST_OPTION_FIELDS[test] ?? []) {
      const { value, error } = parseOption(f, values[test]?.[f.key]);
      if (value !== undefined && !error) opts[f.key] = value;
    }
    if (Object.keys(opts).length) out[test] = opts;
  }
  return Object.keys(out).length ? out : null;
}

/** How many fields of a test are filled in. */
export function filledCount(values: TestOptionValues, test: string): number {
  return Object.values(values[test] ?? {}).filter((v) => v.trim() !== '').length;
}

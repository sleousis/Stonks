import type {
  BacktestRequest,
  LabRunRequest,
  McptOptions,
  StrategyClassInfo,
  WalkForwardConfig,
} from '../../api/models';
import { type ParamValues, paramErrors, paramPayload } from '../../shared/ui/param-form/param-spec';
import { type TestOptionValues, buildTestOptions, testOptionErrors } from './test-options';

/**
 * Form state for the lab page and the pure functions that turn it into API
 * request bodies. Kept free of Angular so the payloads are easy to test.
 */

export type SurvivalTestName = NonNullable<LabRunRequest['survival_tests']>[number];
export type SuitePreset = NonNullable<LabRunRequest['preset']>;
export type SuiteChoice = SuitePreset | 'custom';
export type CostChoice = 'configured' | 'zero' | 'realistic' | 'flat';
/** `default` sends nothing (the server's `[lab] benchmark`). */
export type BenchmarkChoice = 'default' | 'auto' | 'EW' | 'ticker' | 'none';
export type RetuneChoice = 'default' | 'auto' | 'yes' | 'no';

/** Inputs both forms share. */
export interface WindowForm {
  classPath: string;
  tickers: string;
  start: string;
  end: string;
  interval: string;
}

export interface BenchmarkForm {
  benchmark: BenchmarkChoice;
  benchmarkTicker: string;
}

export interface BacktestForm extends WindowForm, BenchmarkForm {
  params: ParamValues;
  cost: CostChoice;
  slippageBps: number | null;
  feePerTrade: number | null;
  initialCash: number | null;
  rebalanceEveryBars: number | null;
}

export interface LabRunForm extends WindowForm, BenchmarkForm {
  tuner: 'grid' | 'random';
  budget: number | null;
  seed: number | null;
  objective: NonNullable<LabRunRequest['objective']>;
  trainRatio: number | null;
  /** Blank = the server's `[lab] embargo_bars` (raised to the strategy's horizon). */
  embargoBars: number | null;
  suite: SuiteChoice;
  /** The tests ticked for a custom suite. */
  tests: readonly SurvivalTestName[];
  // Walk-forward: blank = the test's default.
  wfSplits: number | null;
  wfTestDays: number | null;
  wfAnchored: boolean;
  wfMinWfe: number | null;
  wfMatrix: boolean;
  // Monte Carlo permutation test: blank = the test's (or preset's) default.
  mcptPermutations: number | null;
  mcptMaxP: number | null;
  mcptMetric: NonNullable<McptOptions['metric']> | '';
  mcptRetune: RetuneChoice;
  /** Advanced options per test, as typed (see test-options.ts). */
  testOptions: TestOptionValues;
  register: boolean;
  /** Register only when every test passes (the default); off = always. */
  registerIfPasses: boolean;
  hypothesis: string;
  premortem: string;
}

export interface SurvivalTestInfo {
  id: SurvivalTestName;
  label: string;
  hint: string;
}

/** Every survival test, in the order a suite runs them. */
export const SURVIVAL_TESTS: readonly SurvivalTestInfo[] = [
  { id: 'oos', label: 'Out of sample', hint: 'Score on data the tuner never saw.' },
  {
    id: 'period_stability',
    label: 'Period stability',
    hint: 'Similar results across sub-periods.',
  },
  { id: 'perturbation', label: 'Perturbation', hint: 'Small noise does not break it.' },
  { id: 'walk_forward', label: 'Walk-forward', hint: 'Re-tune and test fold by fold.' },
  {
    id: 'deflated_sharpe',
    label: 'Deflated Sharpe',
    hint: 'The Sharpe survives the number of trials.',
  },
  { id: 'pbo', label: 'Overfitting (PBO)', hint: 'The best trial is not just the luckiest.' },
  { id: 'mc_trades', label: 'Monte Carlo trades', hint: 'Reshuffled trades rarely ruin it.' },
  { id: 'cost_stress', label: 'Cost stress', hint: 'Still works at two or three times the costs.' },
  { id: 'plateau', label: 'Parameter plateau', hint: 'Nearby parameters also work.' },
  { id: 'cross_instrument', label: 'Cross-instrument', hint: 'Works on other tickers too.' },
  { id: 'benchmark_relative', label: 'Beats the benchmark', hint: 'Positive IR and excess CAGR.' },
  { id: 'mcpt', label: 'Monte Carlo permutation', hint: 'Beats shuffled prices (MCPT).' },
  { id: 'drift', label: 'Drift', hint: 'Train and test returns look alike.' },
  { id: 'runs_test', label: 'Runs test', hint: 'Wins and losses are not clustered.' },
  {
    id: 'walk_forward_mcpt',
    label: 'Walk-forward permutation',
    hint: 'The walk-forward result beats shuffled prices. Slow.',
  },
  // Legacy alias of `mcpt`, still in stored reports.
  { id: 'permutation', label: 'Monte Carlo permutation', hint: 'Beats shuffled prices (MCPT).' },
];

/** Tests offered for a custom suite (the legacy alias is hidden). */
export const PICKABLE_TESTS = SURVIVAL_TESTS.filter((t) => t.id !== 'permutation');

export interface SuiteInfo {
  id: SuiteChoice;
  label: string;
  description: string;
  /** Empty for custom. */
  tests: readonly SurvivalTestName[];
}

/** The server's named suites (`SUITE_PRESETS` in lab/survival/registry.py). */
export const SUITES: readonly SuiteInfo[] = [
  {
    id: 'quick',
    label: 'Quick',
    description:
      'The everyday check: does it hold up on held-out data and in each sub-period? Fast.',
    tests: ['oos', 'period_stability'],
  },
  {
    id: 'standard',
    label: 'Standard',
    description:
      'Adds noise, walk-forward, the deflated Sharpe and cost stress. A few minutes on a small universe.',
    tests: [
      'oos',
      'period_stability',
      'perturbation',
      'walk_forward',
      'deflated_sharpe',
      'cost_stress',
    ],
  },
  {
    id: 'promotion',
    label: 'Promotion',
    description:
      'Everything a strategy must survive before it may trade, including overfitting, Monte Carlo, other tickers, the benchmark and a 200-shuffle permutation test. Slow.',
    tests: [
      'oos',
      'walk_forward',
      'deflated_sharpe',
      'pbo',
      'mc_trades',
      'cost_stress',
      'plateau',
      'cross_instrument',
      'benchmark_relative',
      'mcpt',
    ],
  },
  {
    id: 'custom',
    label: 'Custom',
    description: 'Pick the tests yourself.',
    tests: [],
  },
];

export function defaultWindow(today = new Date()): Pick<WindowForm, 'start' | 'end'> {
  const end = new Date(Date.UTC(today.getFullYear(), today.getMonth(), today.getDate()));
  const start = new Date(end);
  start.setUTCFullYear(end.getUTCFullYear() - 1);
  return { start: isoDay(start), end: isoDay(end) };
}

export function defaultBacktestForm(today?: Date): BacktestForm {
  return {
    classPath: '',
    tickers: '',
    ...defaultWindow(today),
    interval: '1d',
    params: {},
    cost: 'configured',
    slippageBps: 5,
    feePerTrade: 0,
    initialCash: 10_000,
    rebalanceEveryBars: 1,
    benchmark: 'default',
    benchmarkTicker: '',
  };
}

export function defaultLabRunForm(today?: Date): LabRunForm {
  return {
    classPath: '',
    tickers: '',
    ...defaultWindow(today),
    interval: '1d',
    tuner: 'random',
    budget: 20,
    seed: 0,
    objective: 'sharpe',
    trainRatio: 0.7,
    embargoBars: null,
    suite: 'quick',
    tests: ['oos', 'period_stability'],
    wfSplits: null,
    wfTestDays: null,
    wfAnchored: false,
    wfMinWfe: null,
    wfMatrix: false,
    mcptPermutations: null,
    mcptMaxP: null,
    mcptMetric: '',
    mcptRetune: 'default',
    testOptions: {},
    register: false,
    registerIfPasses: true,
    hypothesis: '',
    premortem: '',
    benchmark: 'default',
    benchmarkTicker: '',
  };
}

/** The tests the run will do, in suite order. */
export function suiteTests(f: Pick<LabRunForm, 'suite' | 'tests'>): SurvivalTestName[] {
  if (f.suite !== 'custom') return [...(SUITES.find((s) => s.id === f.suite)?.tests ?? [])];
  return PICKABLE_TESTS.map((t) => t.id).filter((id) => f.tests.includes(id));
}

/** "aapl.us, msft.us\nSPY.US" → ["AAPL.US", "MSFT.US", "SPY.US"] (deduplicated, in order). */
export function parseTickers(text: string): string[] {
  const seen = new Set<string>();
  for (const raw of text.split(/[\s,;]+/)) {
    const t = raw.trim().toUpperCase();
    if (t) seen.add(t);
  }
  return [...seen];
}

/** The request's `benchmark`, or `undefined` to use the server's default. */
export function benchmarkValue(f: BenchmarkForm): string | undefined {
  switch (f.benchmark) {
    case 'default':
      return undefined;
    case 'ticker':
      return f.benchmarkTicker.trim().toUpperCase() || undefined;
    default:
      return f.benchmark;
  }
}

export type FormErrors = Partial<Record<string, string>>;

const TICKER_RE = /^[A-Z0-9^][A-Z0-9.\-^=_]{0,31}$/;

function benchmarkErrors(f: BenchmarkForm): FormErrors {
  if (f.benchmark !== 'ticker') return {};
  const t = f.benchmarkTicker.trim().toUpperCase();
  if (!t) return { benchmarkTicker: 'Enter a ticker, e.g. QQQ.US.' };
  if (!TICKER_RE.test(t)) return { benchmarkTicker: 'One ticker, like QQQ.US.' };
  return {};
}

function windowErrors(f: WindowForm): FormErrors {
  const e: FormErrors = {};
  if (!f.classPath) e['strategy'] = 'Pick a strategy class.';
  if (parseTickers(f.tickers).length === 0) e['tickers'] = 'Enter at least one ticker.';
  if (!f.start) e['start'] = 'Pick a start date.';
  if (!f.end) e['end'] = 'Pick an end date.';
  if (f.start && f.end && f.start >= f.end) e['end'] = 'End must be after start.';
  if (!f.interval) e['interval'] = 'Pick an interval.';
  return e;
}

/** Field → message; `param.<name>` keys are parameter errors. */
export function backtestErrors(f: BacktestForm, cls: StrategyClassInfo | null): FormErrors {
  const e = { ...windowErrors(f), ...benchmarkErrors(f) };
  if (cls) {
    for (const [name, msg] of Object.entries(paramErrors(cls.parameters, f.params))) {
      e[`param.${name}`] = msg;
    }
  }
  if (!isNum(f.initialCash) || f.initialCash <= 0) e['initialCash'] = 'Enter a positive amount.';
  if (!isInt(f.rebalanceEveryBars) || f.rebalanceEveryBars < 1)
    e['rebalanceEveryBars'] = 'Enter a whole number of at least 1.';
  if (f.cost === 'flat') {
    if (!isNum(f.slippageBps) || f.slippageBps < 0) e['slippageBps'] = 'Enter 0 or more.';
    if (!isNum(f.feePerTrade) || f.feePerTrade < 0) e['feePerTrade'] = 'Enter 0 or more.';
  }
  return e;
}

/** Field → message; `opt.<test>.<field>` keys are advanced test options. */
export function labRunErrors(f: LabRunForm): FormErrors {
  const e = { ...windowErrors(f), ...benchmarkErrors(f) };
  const tests = suiteTests(f);
  if (!isInt(f.budget) || f.budget < 1 || f.budget > 1000) e['budget'] = 'Between 1 and 1000.';
  if (!isInt(f.seed)) e['seed'] = 'Enter a whole number.';
  if (!isNum(f.trainRatio) || f.trainRatio <= 0 || f.trainRatio >= 1)
    e['trainRatio'] = 'Between 0 and 1, e.g. 0.7.';
  if (
    f.embargoBars !== null &&
    (!isInt(f.embargoBars) || f.embargoBars < 0 || f.embargoBars > 10_000)
  )
    e['embargoBars'] = 'Leave blank or enter 0 to 10000 bars.';
  if (tests.length === 0) e['tests'] = 'Pick at least one survival test.';
  if (tests.includes('walk_forward')) {
    if (f.wfSplits !== null && (!isInt(f.wfSplits) || f.wfSplits < 2))
      e['wfSplits'] = 'Leave blank or enter 2 or more.';
    if (f.wfTestDays !== null && (!isInt(f.wfTestDays) || f.wfTestDays < 1))
      e['wfTestDays'] = 'Leave blank or enter whole days.';
    if (f.wfMinWfe !== null && (!isNum(f.wfMinWfe) || f.wfMinWfe < 0 || f.wfMinWfe > 2))
      e['wfMinWfe'] = 'Leave blank or enter 0 to 2, e.g. 0.5.';
  }
  if (tests.includes('mcpt')) {
    if (
      f.mcptPermutations !== null &&
      (!isInt(f.mcptPermutations) || f.mcptPermutations < 1 || f.mcptPermutations > 1000)
    )
      e['mcptPermutations'] = 'Leave blank or enter 1 to 1000.';
    if (f.mcptMaxP !== null && (!isNum(f.mcptMaxP) || f.mcptMaxP <= 0 || f.mcptMaxP > 1))
      e['mcptMaxP'] = 'Above 0 and at most 1.';
  }
  if (f.register && !f.hypothesis.trim())
    e['hypothesis'] = 'Say why it should make money before registering it.';
  if (f.hypothesis.length > 4000) e['hypothesis'] = 'At most 4000 characters.';
  if (f.premortem.length > 4000) e['premortem'] = 'At most 4000 characters.';
  for (const [key, msg] of Object.entries(testOptionErrors(tests, f.testOptions))) {
    e[`opt.${key}`] = msg;
  }
  return e;
}

export function buildBacktestRequest(
  f: BacktestForm,
  cls: StrategyClassInfo | null,
): BacktestRequest {
  const body: BacktestRequest = {
    strategy: {
      class_path: f.classPath,
      params: cls ? paramPayload(cls.parameters, f.params) : {},
    },
    universe: parseTickers(f.tickers),
    start: f.start,
    end: f.end,
    interval: f.interval,
    initial_cash: f.initialCash ?? 10_000,
    rebalance_every_bars: f.rebalanceEveryBars ?? 1,
  };
  // A preset and flat costs are mutually exclusive; "configured" sends neither.
  if (f.cost === 'zero' || f.cost === 'realistic') body.cost_model = f.cost;
  if (f.cost === 'flat') {
    body.slippage_bps = f.slippageBps ?? 0;
    body.fee_per_trade = f.feePerTrade ?? 0;
  }
  const benchmark = benchmarkValue(f);
  if (benchmark) body.benchmark = benchmark;
  return body;
}

export function buildLabRunRequest(f: LabRunForm): LabRunRequest {
  const tests = suiteTests(f);
  const body: LabRunRequest = {
    // The tuner searches the class's parameter space; params are ignored.
    strategy: { class_path: f.classPath },
    universe: parseTickers(f.tickers),
    start: f.start,
    end: f.end,
    interval: f.interval,
    tuner: f.tuner,
    budget: f.budget ?? 20,
    seed: f.seed ?? 0,
    objective: f.objective,
    train_ratio: f.trainRatio ?? 0.7,
  };
  if (f.suite === 'custom') body.survival_tests = tests;
  else body.preset = f.suite;

  if (f.embargoBars !== null) body.embargo_bars = f.embargoBars;
  const benchmark = benchmarkValue(f);
  if (benchmark) body.benchmark = benchmark;

  if (f.register) {
    if (f.registerIfPasses) body.register_if_passes = true;
    else body.register_strategy = true;
  }
  const hypothesis = f.hypothesis.trim();
  const premortem = f.premortem.trim();
  if (hypothesis) body.hypothesis = hypothesis;
  if (premortem) body.premortem = premortem;

  if (tests.includes('walk_forward')) {
    const wf: WalkForwardConfig = {};
    if (f.wfSplits !== null) wf.n_splits = f.wfSplits;
    if (f.wfTestDays !== null) wf.test_days = f.wfTestDays;
    if (f.wfAnchored) wf.anchored = true;
    if (f.wfMinWfe !== null) wf.min_wfe = f.wfMinWfe;
    if (f.wfMatrix) wf.matrix = true;
    // Only send a config when something differs from the test's defaults.
    if (Object.keys(wf).length) body.walk_forward = { ...wf, metric: f.objective };
  }
  if (tests.includes('mcpt')) {
    const mcpt: McptOptions = {};
    if (f.mcptPermutations !== null) mcpt.n_permutations = f.mcptPermutations;
    if (f.mcptMaxP !== null) mcpt.max_p_value = f.mcptMaxP;
    if (f.mcptMetric) mcpt.metric = f.mcptMetric;
    if (f.mcptRetune !== 'default')
      mcpt.retune = f.mcptRetune === 'auto' ? 'auto' : f.mcptRetune === 'yes';
    if (Object.keys(mcpt).length) body.mcpt = mcpt;
  }
  const options = buildTestOptions(tests, f.testOptions);
  if (options) body.test_options = options;
  return body;
}

/** Catalog grouped for the picker: by source package, then name. */
export interface StrategyGroup {
  label: string;
  classes: StrategyClassInfo[];
}

export function groupStrategies(
  classes: readonly StrategyClassInfo[],
  query = '',
): StrategyGroup[] {
  const q = query.trim().toLowerCase();
  const groups = new Map<string, StrategyClassInfo[]>();
  for (const c of classes) {
    if (q && !`${c.name} ${c.description} ${c.class_path}`.toLowerCase().includes(q)) continue;
    const label = groupLabel(c);
    const list = groups.get(label) ?? [];
    list.push(c);
    groups.set(label, list);
  }
  return [...groups.entries()]
    .sort(([a], [b]) => a.localeCompare(b))
    .map(([label, list]) => ({
      label,
      classes: list.sort((a, b) => a.name.localeCompare(b.name)),
    }));
}

/**
 * `stonks.strategies.examples.momentum:Momentum` → "Examples"; a class at the
 * package root (`stonks.strategies.macro_regime:X`) → "Strategies"; classes
 * from another source are grouped under the source name.
 */
export function groupLabel(c: StrategyClassInfo): string {
  const modules = c.class_path.split(':')[0].split('.');
  const i = modules.indexOf('strategies');
  if (i >= 0) {
    const pkg = modules.length - i > 2 ? modules[i + 1] : 'strategies';
    return titleCase(pkg);
  }
  return titleCase(c.source || modules[0] || 'Other');
}

function titleCase(s: string): string {
  const words = s.replace(/[_-]+/g, ' ').trim();
  return words ? words.charAt(0).toUpperCase() + words.slice(1) : s;
}

function isNum(v: number | null): v is number {
  return typeof v === 'number' && Number.isFinite(v);
}

function isInt(v: number | null): v is number {
  return isNum(v) && Number.isInteger(v);
}

function isoDay(d: Date): string {
  return d.toISOString().slice(0, 10);
}

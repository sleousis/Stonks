import type {
  BacktestRequest,
  CostModelPreset,
  LabRunRequest,
  McptOptions,
  StrategyClassInfo,
  SurvivalPresetInfo,
  WalkForwardConfig,
} from '../../api/models';
import { type ParamValues, paramErrors, paramPayload } from '../../shared/ui/param-form/param-spec';
import { SURVIVAL_TESTS, type SurvivalTestName } from '../../shared/lab-results/survival-tests';
import {
  type OptionCatalog,
  type TestOptionValues,
  buildTestOptions,
  testOptionErrors,
} from './test-options';

/**
 * Form state for the lab page and the pure functions that turn it into API
 * request bodies. Kept free of Angular so the payloads are easy to test.
 */

export type { SurvivalTestInfo, SurvivalTestName } from '../../shared/lab-results/survival-tests';
export { SURVIVAL_TESTS } from '../../shared/lab-results/survival-tests';
export type SuitePreset = NonNullable<LabRunRequest['preset']>;
export type SuiteChoice = SuitePreset | 'custom';
export type CostChoice = 'configured' | 'zero' | 'realistic' | 'flat';
/** `default` sends nothing: the server's default benchmark. */
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

/** The cost field the Lab backtest and the Studio share (`cost-field.ts`). */
export interface CostForm {
  cost: CostChoice;
  slippageBps: number | null;
  feePerTrade: number | null;
}

export interface BacktestForm extends WindowForm, BenchmarkForm, CostForm {
  params: ParamValues;
  initialCash: number | null;
  rebalanceEveryBars: number | null;
}

export type TunerChoice = NonNullable<LabRunRequest['tuner']>;
export type SamplerChoice = NonNullable<LabRunRequest['sampler']>;

/** Heatmap grid bounds (points per axis), as the API checks them. */
export const HEATMAP_GRID_MIN = 2;
export const HEATMAP_GRID_MAX = 15;

export interface LabRunForm extends WindowForm, BenchmarkForm {
  tuner: TunerChoice;
  /** Optuna only: how it searches. */
  sampler: SamplerChoice;
  /** Optuna only: stop trials early that trail on the fast path. */
  prune: boolean;
  /** Sweep two parameters around the tuned set after tuning. */
  heatmap: boolean;
  /** '' = the server picks (the first numeric tunable parameter). */
  heatmapX: string;
  heatmapY: string;
  heatmapGrid: number | null;
  /** Score the cells with full backtests instead of the fast path. */
  heatmapFull: boolean;
  budget: number | null;
  seed: number | null;
  objective: NonNullable<LabRunRequest['objective']>;
  trainRatio: number | null;
  /** Blank = the server's default gap (raised to the strategy's horizon). */
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
  /** A stored universe to run on instead of the typed tickers ('' = tickers). */
  universeId: string;
  /** Fetch the bars the run needs that our data lacks, first (a stored universe only). */
  ensureData: boolean;
}

export const PICKABLE_TESTS = SURVIVAL_TESTS.filter((t) => t.id !== 'permutation');

export interface SuiteInfo {
  id: SuiteChoice;
  label: string;
  description: string;
  /** Empty for custom. */
  tests: readonly SurvivalTestName[];
}

/**
 * The named suites with the console's words for them. The tests here are a
 * fallback while `GET /api/lab/survival-presets` loads: `suitesFromPresets`
 * puts the server's lists in.
 */
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
    label: 'Go-live',
    description:
      'Everything the go-live check asks a strategy to pass, including overfitting, Monte Carlo, other tickers, the benchmark and a 200-shuffle permutation test. Slow.',
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

const KNOWN_TESTS = new Set<string>(SURVIVAL_TESTS.map((t) => t.id));

/**
 * `suites` with each named suite's tests taken from the API's presets, so
 * the form shows what the server will really run. Suites the API does not
 * name keep their fallback tests; tests the console has no words for yet
 * are left out of the list (the server still runs them).
 */
export function suitesFromPresets(
  presets: readonly SurvivalPresetInfo[],
  suites: readonly SuiteInfo[] = SUITES,
): SuiteInfo[] {
  return suites.map((s) => {
    const preset = presets.find((p) => p.name === s.id);
    if (s.id === 'custom' || !preset) return s;
    const tests = preset.tests.filter((t): t is SurvivalTestName => KNOWN_TESTS.has(t));
    return { ...s, tests };
  });
}

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
    sampler: 'tpe',
    prune: false,
    heatmap: false,
    heatmapX: '',
    heatmapY: '',
    heatmapGrid: 7,
    heatmapFull: false,
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
    universeId: '',
    ensureData: false,
  };
}

/** The tests the run will do, in suite order. */
export function suiteTests(
  f: Pick<LabRunForm, 'suite' | 'tests'>,
  suites: readonly SuiteInfo[] = SUITES,
): SurvivalTestName[] {
  if (f.suite !== 'custom') return [...(suites.find((s) => s.id === f.suite)?.tests ?? [])];
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

export function windowErrors(f: WindowForm): FormErrors {
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
  return { ...e, ...costErrors(f) };
}

/** `slippageBps` / `feePerTrade` messages when flat costs are picked. */
export function costErrors(f: CostForm): FormErrors {
  const e: FormErrors = {};
  if (f.cost === 'flat') {
    if (!isNum(f.slippageBps) || f.slippageBps < 0) e['slippageBps'] = 'Enter 0 or more.';
    if (!isNum(f.feePerTrade) || f.feePerTrade < 0) e['feePerTrade'] = 'Enter 0 or more.';
  }
  return e;
}

/**
 * The cost part of a backtest body. A preset and flat costs are mutually
 * exclusive, and "configured" sends neither: the server then charges the
 * costs the admin set up for backtests (P18: costs are on by default).
 */
export function costFields(
  f: CostForm,
): Pick<BacktestRequest, 'cost_model' | 'slippage_bps' | 'fee_per_trade'> {
  if (f.cost === 'zero' || f.cost === 'realistic') return { cost_model: f.cost };
  if (f.cost === 'flat')
    return { slippage_bps: f.slippageBps ?? 0, fee_per_trade: f.feePerTrade ?? 0 };
  return {};
}

/** One line under the cost field saying what the choice charges. */
export function costHint(cost: CostChoice, presets: readonly CostModelPreset[]): string {
  if (cost === 'configured') return 'The fees and slippage your admin set up for backtests.';
  if (cost === 'flat') return 'A flat slippage on every fill and a fixed fee per trade.';
  return presets.find((m) => m.name === cost)?.description ?? '';
}

/**
 * Field → message; `opt.<test>.<field>` keys are advanced test options,
 * checked against `catalog` (from `GET /api/lab/survival-tests`).
 */
export function labRunErrors(
  f: LabRunForm,
  catalog: OptionCatalog = {},
  suites: readonly SuiteInfo[] = SUITES,
): FormErrors {
  const e = { ...windowErrors(f), ...benchmarkErrors(f) };
  // A stored universe replaces the typed tickers.
  if (f.universeId) delete e['tickers'];
  const tests = suiteTests(f, suites);
  if (!isInt(f.budget) || f.budget < 1 || f.budget > 1000) e['budget'] = 'Between 1 and 1000.';
  if (!isInt(f.seed)) e['seed'] = 'Enter a whole number.';
  if (!isNum(f.trainRatio) || f.trainRatio <= 0 || f.trainRatio >= 1)
    e['trainRatio'] = 'Between 0 and 1, e.g. 0.7.';
  if (
    f.embargoBars !== null &&
    (!isInt(f.embargoBars) || f.embargoBars < 0 || f.embargoBars > 10_000)
  )
    e['embargoBars'] = 'Leave blank or enter 0 to 10000 bars.';
  if (f.heatmap) {
    if (
      !isInt(f.heatmapGrid) ||
      f.heatmapGrid < HEATMAP_GRID_MIN ||
      f.heatmapGrid > HEATMAP_GRID_MAX
    )
      e['heatmapGrid'] = `Between ${HEATMAP_GRID_MIN} and ${HEATMAP_GRID_MAX}.`;
    if (f.heatmapX && f.heatmapX === f.heatmapY) e['heatmapY'] = 'Pick a different parameter.';
  }
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
    e['hypothesis'] = 'Say why it should make money before it starts paper trading.';
  if (f.hypothesis.length > 4000) e['hypothesis'] = 'At most 4000 characters.';
  if (f.premortem.length > 4000) e['premortem'] = 'At most 4000 characters.';
  for (const [key, msg] of Object.entries(testOptionErrors(tests, f.testOptions, catalog))) {
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
  Object.assign(body, costFields(f));
  const benchmark = benchmarkValue(f);
  if (benchmark) body.benchmark = benchmark;
  return body;
}

export function buildLabRunRequest(
  f: LabRunForm,
  catalog: OptionCatalog = {},
  suites: readonly SuiteInfo[] = SUITES,
): LabRunRequest {
  const tests = suiteTests(f, suites);
  const body: LabRunRequest = {
    // The tuner searches the class's parameter space; params are ignored.
    strategy: { class_path: f.classPath },
    start: f.start,
    end: f.end,
    interval: f.interval,
    tuner: f.tuner,
    budget: f.budget ?? 20,
    seed: f.seed ?? 0,
    objective: f.objective,
    train_ratio: f.trainRatio ?? 0.7,
  };
  if (f.universeId) {
    body.universe_id = f.universeId;
    if (f.ensureData) body.ensure_data = true;
  } else {
    body.universe = parseTickers(f.tickers);
  }
  if (f.tuner === 'optuna') {
    body.sampler = f.sampler;
    if (f.prune) body.prune = true;
  }
  if (f.heatmap) {
    body.heatmap = {
      x: f.heatmapX || null,
      y: f.heatmapY || null,
      grid_size: f.heatmapGrid ?? 7,
      fast: !f.heatmapFull,
    };
  }
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
    // A cv_ objective scores walk-forward on its plain metric. Walk-forward
    // scores only a few metrics: other objectives keep the test's default.
    const metric = f.objective.replace(/^cv_/, '');
    if (WF_METRICS.includes(metric as WfMetric)) wf.metric = metric as WfMetric;
    const shaped = Object.keys(wf).filter((k) => k !== 'metric');
    if (shaped.length) body.walk_forward = wf;
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
  const options = buildTestOptions(tests, f.testOptions, catalog);
  if (options) body.test_options = options;
  return body;
}

type WfMetric = NonNullable<WalkForwardConfig['metric']>;
const WF_METRICS: readonly WfMetric[] = ['sharpe', 'cagr', 'final_return'];

/** Every objective the lab-run form offers, in menu order. */
export const OBJECTIVES: readonly {
  id: NonNullable<LabRunRequest['objective']>;
  label: string;
}[] = [
  { id: 'sharpe', label: 'Sharpe' },
  { id: 'cagr', label: 'CAGR' },
  { id: 'final_return', label: 'Total return' },
  { id: 'sortino', label: 'Sortino' },
  { id: 'calmar', label: 'Calmar' },
  { id: 'sharpe_dd', label: 'Sharpe less twice the drawdown' },
  { id: 'multi', label: 'Sharpe, Calmar and drawdown together' },
  { id: 'cv_sharpe', label: 'Sharpe on purged folds' },
  { id: 'cv_cagr', label: 'CAGR on purged folds' },
  { id: 'cv_final_return', label: 'Total return on purged folds' },
];

/**
 * The form fields a stored lab-run request (a job's `params`) fills, for a
 * prefilled re-run. Unknown or odd values are left out, so the form keeps
 * its defaults there. Advanced per-test options are not carried over.
 */
export function formFromRequest(
  request: LabRunRequest | Readonly<Record<string, unknown>>,
): Partial<LabRunForm> {
  const r = request as Readonly<Record<string, unknown>>;
  const f: Partial<LabRunForm> = {};
  const str = (v: unknown): v is string => typeof v === 'string';
  const num = (v: unknown): v is number => typeof v === 'number' && Number.isFinite(v);
  const strategy = r['strategy'];
  if (strategy && typeof strategy === 'object') {
    const cp = (strategy as Record<string, unknown>)['class_path'];
    if (str(cp)) f.classPath = cp;
  }
  if (str(r['start'])) f.start = r['start'];
  if (str(r['end'])) f.end = r['end'];
  if (str(r['interval'])) f.interval = r['interval'];
  if (str(r['universe_id']) && r['universe_id']) {
    f.universeId = r['universe_id'];
    f.ensureData = r['ensure_data'] === true;
  } else if (Array.isArray(r['universe'])) {
    f.tickers = r['universe'].filter(str).join(', ');
  }
  if (r['tuner'] === 'grid' || r['tuner'] === 'random' || r['tuner'] === 'optuna')
    f.tuner = r['tuner'];
  if (r['sampler'] === 'tpe' || r['sampler'] === 'nsga2' || r['sampler'] === 'random')
    f.sampler = r['sampler'];
  if (r['prune'] === true) f.prune = true;
  const heatmap = r['heatmap'];
  if (heatmap && typeof heatmap === 'object') {
    const h = heatmap as Record<string, unknown>;
    f.heatmap = true;
    if (str(h['x'])) f.heatmapX = h['x'];
    if (str(h['y'])) f.heatmapY = h['y'];
    if (num(h['grid_size'])) f.heatmapGrid = h['grid_size'];
    if (h['fast'] === false) f.heatmapFull = true;
  }
  if (num(r['budget'])) f.budget = r['budget'];
  if (num(r['seed'])) f.seed = r['seed'];
  if (num(r['train_ratio'])) f.trainRatio = r['train_ratio'];
  if (num(r['embargo_bars'])) f.embargoBars = r['embargo_bars'];
  const objectives: readonly string[] = OBJECTIVES.map((o) => o.id);
  if (str(r['objective']) && objectives.includes(r['objective']))
    f.objective = r['objective'] as LabRunForm['objective'];
  const preset = r['preset'];
  if (preset === 'quick' || preset === 'standard' || preset === 'promotion') {
    f.suite = preset;
  } else if (Array.isArray(r['survival_tests'])) {
    f.suite = 'custom';
    f.tests = r['survival_tests'].filter((t): t is SurvivalTestName => KNOWN_TESTS.has(t));
  }
  const benchmark = r['benchmark'];
  if (benchmark === 'auto' || benchmark === 'EW' || benchmark === 'none') {
    f.benchmark = benchmark;
  } else if (str(benchmark) && benchmark) {
    f.benchmark = 'ticker';
    f.benchmarkTicker = benchmark;
  }
  if (r['register_if_passes'] === true || r['register_strategy'] === true) {
    f.register = true;
    f.registerIfPasses = r['register_if_passes'] === true;
  }
  if (str(r['hypothesis'])) f.hypothesis = r['hypothesis'];
  if (str(r['premortem'])) f.premortem = r['premortem'];
  const wf = r['walk_forward'];
  if (wf && typeof wf === 'object') {
    const w = wf as Record<string, unknown>;
    if (num(w['n_splits'])) f.wfSplits = w['n_splits'];
    if (num(w['test_days'])) f.wfTestDays = w['test_days'];
    if (num(w['min_wfe'])) f.wfMinWfe = w['min_wfe'];
    if (w['anchored'] === true) f.wfAnchored = true;
    if (w['matrix'] === true) f.wfMatrix = true;
  }
  const mcpt = r['mcpt'];
  if (mcpt && typeof mcpt === 'object') {
    const m = mcpt as Record<string, unknown>;
    if (num(m['n_permutations'])) f.mcptPermutations = m['n_permutations'];
    if (num(m['max_p_value'])) f.mcptMaxP = m['max_p_value'];
    const metrics: readonly string[] = ['profit_factor', 'sharpe', 'cagr', 'final_return'];
    if (str(m['metric']) && metrics.includes(m['metric']))
      f.mcptMetric = m['metric'] as LabRunForm['mcptMetric'];
    if (m['retune'] === 'auto') f.mcptRetune = 'auto';
    else if (m['retune'] === true) f.mcptRetune = 'yes';
    else if (m['retune'] === false) f.mcptRetune = 'no';
  }
  return f;
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

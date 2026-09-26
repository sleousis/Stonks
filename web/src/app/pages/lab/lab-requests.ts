import type {
  BacktestRequest,
  LabRunRequest,
  McptOptions,
  StrategyClassInfo,
  WalkForwardConfig,
} from '../../api/models';
import { type ParamValues, paramErrors, paramPayload } from '../../shared/ui/param-form/param-spec';

/**
 * Form state for the lab page and the pure functions that turn it into API
 * request bodies. Kept free of Angular so the payloads are easy to test.
 */

export type SurvivalTestName = NonNullable<LabRunRequest['survival_tests']>[number];
export type CostChoice = 'configured' | 'zero' | 'realistic' | 'flat';

/** Inputs both forms share. */
export interface WindowForm {
  classPath: string;
  tickers: string;
  start: string;
  end: string;
  interval: string;
}

export interface BacktestForm extends WindowForm {
  params: ParamValues;
  cost: CostChoice;
  slippageBps: number | null;
  feePerTrade: number | null;
  initialCash: number | null;
  rebalanceEveryBars: number | null;
}

export interface LabRunForm extends WindowForm {
  tuner: 'grid' | 'random';
  budget: number | null;
  seed: number | null;
  objective: NonNullable<LabRunRequest['objective']>;
  trainRatio: number | null;
  tests: readonly SurvivalTestName[];
  wfSplits: number | null;
  /** Blank = split the validation window evenly over the folds. */
  wfTestDays: number | null;
  wfAnchored: boolean;
  mcptPermutations: number | null;
  mcptMaxP: number | null;
  mcptMetric: NonNullable<McptOptions['metric']>;
  mcptRetune: boolean;
  register: boolean;
}

export const SURVIVAL_TESTS: readonly { id: SurvivalTestName; label: string; hint: string }[] = [
  { id: 'oos', label: 'Out of sample', hint: 'Score on data the tuner never saw.' },
  {
    id: 'period_stability',
    label: 'Period stability',
    hint: 'Similar results across sub-periods.',
  },
  { id: 'perturbation', label: 'Perturbation', hint: 'Nearby parameters still work.' },
  { id: 'drift', label: 'Drift', hint: 'Train and test returns look alike.' },
  { id: 'runs_test', label: 'Runs test', hint: 'Wins and losses are not clustered.' },
  { id: 'walk_forward', label: 'Walk-forward', hint: 'Re-tune and test fold by fold.' },
  { id: 'permutation', label: 'Monte Carlo permutation', hint: 'Beats shuffled prices (MCPT).' },
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
    tests: ['oos', 'period_stability'],
    wfSplits: 4,
    wfTestDays: null,
    wfAnchored: false,
    mcptPermutations: 50,
    mcptMaxP: 0.05,
    mcptMetric: 'profit_factor',
    mcptRetune: false,
    register: false,
  };
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

export type FormErrors = Partial<Record<string, string>>;

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
  const e = windowErrors(f);
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

export function labRunErrors(f: LabRunForm): FormErrors {
  const e = windowErrors(f);
  if (!isInt(f.budget) || f.budget < 1 || f.budget > 1000) e['budget'] = 'Between 1 and 1000.';
  if (!isInt(f.seed)) e['seed'] = 'Enter a whole number.';
  if (!isNum(f.trainRatio) || f.trainRatio <= 0 || f.trainRatio >= 1)
    e['trainRatio'] = 'Between 0 and 1, e.g. 0.7.';
  if (f.tests.length === 0) e['tests'] = 'Pick at least one survival test.';
  if (f.tests.includes('walk_forward')) {
    if (!isInt(f.wfSplits) || f.wfSplits < 1) e['wfSplits'] = 'Enter a whole number of at least 1.';
    if (f.wfTestDays !== null && (!isInt(f.wfTestDays) || f.wfTestDays < 1))
      e['wfTestDays'] = 'Leave blank or enter whole days.';
  }
  if (f.tests.includes('permutation')) {
    if (!isInt(f.mcptPermutations) || f.mcptPermutations < 1 || f.mcptPermutations > 1000)
      e['mcptPermutations'] = 'Between 1 and 1000.';
    if (!isNum(f.mcptMaxP) || f.mcptMaxP <= 0 || f.mcptMaxP > 1)
      e['mcptMaxP'] = 'Above 0 and at most 1.';
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
  return body;
}

export function buildLabRunRequest(f: LabRunForm): LabRunRequest {
  // Keep the canonical order so requests are stable whatever order boxes were ticked.
  const tests = SURVIVAL_TESTS.map((t) => t.id).filter((id) => f.tests.includes(id));
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
    survival_tests: tests,
    register_strategy: f.register,
  };
  if (tests.includes('walk_forward')) {
    const wf: WalkForwardConfig = {
      n_splits: f.wfSplits ?? 4,
      anchored: f.wfAnchored,
      metric: f.objective,
    };
    if (f.wfTestDays !== null) wf.test_days = f.wfTestDays;
    body.walk_forward = wf;
  }
  if (tests.includes('permutation')) {
    body.mcpt = {
      n_permutations: f.mcptPermutations ?? 50,
      max_p_value: f.mcptMaxP ?? 0.05,
      metric: f.mcptMetric,
      retune: f.mcptRetune,
    };
  }
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

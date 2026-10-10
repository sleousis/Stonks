import type { LabRunRequest } from '../../api/models';
import { humanize } from '../ui/param-form/param-spec';

export type SurvivalTestName = NonNullable<LabRunRequest['survival_tests']>[number];

export interface SurvivalTestInfo {
  id: SurvivalTestName;
  /** A plain name ("Higher costs"). */
  label: string;
  /** One line: what the test guards against, then how. */
  hint: string;
  /** Slow tests say so in the picker. */
  slow?: boolean;
  /** Reported for information only: it never fails a run. */
  informational?: boolean;
}

/**
 * Every robustness test the engine runs (24, plus a legacy alias), in the
 * order a suite runs them. The server's `stonks.lab.survival.registry`
 * discovers them; `production/golive.py` names them the same way.
 */
export const SURVIVAL_TESTS: readonly SurvivalTestInfo[] = [
  {
    id: 'oos',
    label: 'Out of sample',
    hint: 'Guards against settings that only fit the past: scores the winner on data the search never saw.',
  },
  {
    id: 'period_stability',
    label: 'Period stability',
    hint: 'Guards against one lucky stretch carrying the result: checks each sub-period on its own.',
  },
  {
    id: 'perturbation',
    label: 'Noisy prices',
    hint: 'Guards against a fragile edge: adds small noise to the prices and checks the results hold.',
  },
  {
    id: 'walk_forward',
    label: 'Rolling retest',
    hint: 'Guards against settings that go stale: tunes on the past, tests on the next slice, and repeats.',
    slow: true,
  },
  {
    id: 'deflated_sharpe',
    label: 'Luck from many tries',
    hint: 'Guards against a good score found by trying many settings: discounts the score for every try.',
  },
  {
    id: 'pbo',
    label: 'Picked the luckiest setting',
    hint: 'Guards against picking the luckiest setting: measures how often the best one loses on new data.',
  },
  {
    id: 'data_snooping',
    label: 'Beats cash after many tries',
    hint: 'Guards against a winner found by trying many settings: the best one must still beat cash once every try is counted.',
  },
  {
    id: 'mc_trades',
    label: 'Reshuffled trades',
    hint: 'Guards against a lucky order of trades: reshuffles them to see how deep the drops could get.',
  },
  {
    id: 'cost_stress',
    label: 'Higher costs',
    hint: 'Guards against an edge that costs eat: runs again at two and three times the trading costs.',
  },
  {
    id: 'plateau',
    label: 'Nearby settings',
    hint: 'Guards against one magic value: checks that settings close to the winner also work.',
  },
  {
    id: 'cross_instrument',
    label: 'Other tickers',
    hint: 'Guards against an edge that lives in one name: runs on each ticker and on held-out ones.',
  },
  {
    id: 'benchmark_relative',
    label: 'Beats the benchmark',
    hint: 'Guards against paying for plain market moves: it must beat simply holding the benchmark.',
  },
  {
    id: 'mcpt',
    label: 'Shuffled prices',
    hint: 'Guards against patterns found in noise: it must beat the same strategy on shuffled prices.',
    slow: true,
  },
  {
    id: 'event_study',
    label: 'After each signal',
    hint: 'Guards against signals that do nothing: prices must move after its entries beyond their usual drift.',
  },
  {
    id: 'vs_random',
    label: 'Beats random data',
    hint: 'Guards against a search that finds a winner anywhere: the same search on random look-alike prices must find clearly less.',
    slow: true,
  },
  {
    id: 'cpcv',
    label: 'Many data splits',
    hint: 'Guards against one lucky split of the data: tests on many train and test splits it never saw.',
    slow: true,
  },
  {
    id: 'crisis',
    label: 'Past crises',
    hint: "Guards against a strategy that breaks in a crash: its drops in past crises must stay near the benchmark's.",
  },
  {
    id: 'signal_ic',
    label: 'Signal ranking',
    hint: 'Shows how well its scores ranked the moves that followed. For information: it never fails a run.',
    informational: true,
  },
  {
    id: 'drift',
    label: 'Drift in the data',
    hint: 'Guards against test data unlike the training data: compares how the inputs are spread in each.',
  },
  {
    id: 'runs_test',
    label: 'Win and loss streaks',
    hint: 'Guards against results that come in clumps: checks wins and losses are spread out.',
  },
  {
    id: 'stress',
    label: 'Rough conditions',
    hint: 'Guards against a strategy that only works in good times: simulates many test periods and checks the bad ones.',
    slow: true,
  },
  {
    id: 'pool_correlation',
    label: 'Adds something new',
    hint: 'Guards against copying what already runs: its returns must not move with the approved strategies.',
  },
  {
    id: 'walk_forward_mcpt',
    label: 'Rolling retest on shuffled prices',
    hint: 'Guards against a rolling retest result that noise could match: repeats it on shuffled prices.',
    slow: true,
  },
  {
    id: 'forecast_skill',
    label: 'Beats simple forecasts',
    hint: 'Guards against a forecasting model with no skill: it must beat simple forecasts, such as tomorrow equals today.',
  },
  // Legacy alias of `mcpt`, still in stored reports.
  {
    id: 'permutation',
    label: 'Shuffled prices',
    hint: 'Guards against patterns found in noise: it must beat the same strategy on shuffled prices.',
    slow: true,
  },
];

/** How many robustness tests the engine has (the legacy alias not counted). */
export const SURVIVAL_TEST_COUNT = SURVIVAL_TESTS.filter((t) => t.id !== 'permutation').length;

/** A test's info, or `null` for an id the console does not know. */
export function testInfo(id: string): SurvivalTestInfo | null {
  return SURVIVAL_TESTS.find((t) => t.id === id) ?? null;
}

/** A robustness test's display name ("Out of sample"); unknown ids are humanized. */
export function testLabel(id: string): string {
  return testInfo(id)?.label ?? humanize(id);
}

/** What a robustness test guards against, in one line ('' when unknown). */
export function testHint(id: string): string {
  return testInfo(id)?.hint ?? '';
}

const pct = (v: string) => `${(Number(v) * 100).toFixed(1)}%`;

/** Machine notes the engine writes, and how each reads in plain words. */
const NOTE_WORDS: readonly [RegExp, (m: RegExpExecArray) => string][] = [
  [
    /insufficient trades: (\d+) < (\d+)/,
    (m) => `Only ${m[1]} trades on the unseen data, it needs ${m[2]}.`,
  ],
  [
    /insufficient data: (\d+) validation returns < (\d+)/,
    (m) => `Only ${m[1]} days of unseen data, it needs ${m[2]}. Try a longer window.`,
  ],
  [/no trades in the validation window/, () => 'It made no trades on the unseen data.'],
  [/insufficient data/, () => 'There was not enough data for this test. Try a longer window.'],
  [/Sharpe of exactly 0|no trading/, () => 'It never traded in any period.'],
  [
    /max drawdown (-?[\d.]+) below (-?[\d.]+)/,
    (m) => `Its worst drop on the unseen data was ${pct(m[1])}, the limit is ${pct(m[2])}.`,
  ],
  [/^(Sharpe|PSR\(0\)) /, () => 'Its return for the risk taken was too low on the unseen data.'],
  [/^DSR /, () => 'After counting every setting tried, the result could still be luck.'],
  [/break-even multiple/, () => 'Higher trading costs would wipe out its gains.'],
  [/speed limit/, () => 'It trades so often that costs eat too much of its return.'],
  [/borrow fees/, () => 'Higher borrowing fees would wipe out its gains.'],
  [/positive_share/, () => 'Too few of the retests made money.'],
  [/oos_score_mean/, () => 'On average the retests lost money.'],
  [/^wfe /, () => 'It kept too little of its past return in the retests.'],
  [/^matrix/, () => 'It did not hold up across different retest lengths.'],
];

/** Why a test passed, when its notes say nothing plain. */
const PASS_WORDS: Readonly<Record<string, string>> = {
  oos: 'It did well on data the search never saw.',
  period_stability: 'It did about as well in each part of the window.',
  perturbation: 'Small noise in the prices barely changed its results.',
  walk_forward: 'It kept working when retested on each next slice of time.',
  deflated_sharpe: 'The result still stands after counting every setting tried.',
  cost_stress: 'Its gains survive double and triple trading costs.',
  signal_ic: 'For information only. It never fails a run.',
};

/** Why a test failed, when its notes say nothing plain. */
const FAIL_WORDS: Readonly<Record<string, string>> = {
  oos: 'It did not do well enough on data the search never saw.',
  period_stability: 'Its results swung too much from one part of the window to the next.',
  perturbation: 'Small noise in the prices changed its results too much.',
  walk_forward: 'It stopped working when retested on later slices of time.',
  deflated_sharpe: 'After counting every setting tried, the result could still be luck.',
  cost_stress: 'Higher trading costs would wipe out its gains.',
};

/**
 * One plain sentence on why a robustness test passed or failed, from its
 * notes when the engine wrote a known reason, else from the test's id.
 * Raw notes ("window=val", "insufficient trades: 5 < 20") never show.
 */
export function testReason(report: { test_id: string; passed: boolean; notes: string }): string {
  const tickers = /universe has (\d+) tickers?; .*needs at least (\d+)/.exec(report.notes ?? '');
  if (tickers) return `It needs at least ${tickers[2]} tickers, this run had ${tickers[1]}.`;
  const sentences: string[] = [];
  for (const part of (report.notes ?? '').split(/;\s*/)) {
    const note = part.replace(/^failed:\s*/, '').trim();
    const known = NOTE_WORDS.find(([pattern]) => pattern.test(note));
    if (!known) continue;
    const sentence = known[1](known[0].exec(note)!);
    if (!sentences.includes(sentence)) sentences.push(sentence);
  }
  if (sentences.length) return sentences.join(' ');
  if (report.passed) return PASS_WORDS[report.test_id] ?? 'It passed this test.';
  return FAIL_WORDS[report.test_id] ?? 'It did not pass this test.';
}

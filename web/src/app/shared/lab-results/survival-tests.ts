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
 * Every robustness test the engine runs (22, plus a legacy alias), in the
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
    label: 'Walk-forward',
    hint: 'Guards against settings that go stale: tunes on the past, tests on the next slice, and repeats.',
    slow: true,
  },
  {
    id: 'deflated_sharpe',
    label: 'Deflated Sharpe',
    hint: 'Guards against a good score found by trying many settings: discounts the Sharpe for every try.',
  },
  {
    id: 'pbo',
    label: 'Overfitting (PBO)',
    hint: 'Guards against picking the luckiest setting: measures how often the best one loses on new data.',
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
    label: 'Shuffled prices (MCPT)',
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
    label: 'Many splits (CPCV)',
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
    label: 'Signal ranking (IC)',
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
    label: 'Walk-forward permutation',
    hint: 'Guards against a walk-forward result that noise could match: repeats it on shuffled prices.',
    slow: true,
  },
  // Legacy alias of `mcpt`, still in stored reports.
  {
    id: 'permutation',
    label: 'Shuffled prices (MCPT)',
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

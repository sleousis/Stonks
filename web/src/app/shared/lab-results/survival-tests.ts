import type { LabRunRequest } from '../../api/models';
import { humanize } from '../ui/param-form/param-spec';

export type SurvivalTestName = NonNullable<LabRunRequest['survival_tests']>[number];

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

/** A survival test's display name ("Out of sample"); unknown ids are humanized. */
export function testLabel(id: string): string {
  return SURVIVAL_TESTS.find((t) => t.id === id)?.label ?? humanize(id);
}

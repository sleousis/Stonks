import type { GoLiveCheckView, GoLiveReport, StrategyStatus } from '../api/models';
import { formatNumber, formatPercent } from '../core/format/format';

/**
 * One plain verdict per strategy (F33): is it worth following? Built from
 * the go-live check, its trial result and its robustness tests, in words a
 * person who is not a quant can act on. The figures stay under "Details".
 */
export type VerdictLevel = 'worth' | 'promising' | 'not_yet';

export const VERDICT_WORDS: Readonly<Record<VerdictLevel, string>> = {
  worth: 'Worth following',
  promising: 'Promising, needs more data',
  not_yet: 'Not good enough yet',
};

/** The status pill tone for each verdict (text and a mark too, never colour alone). */
export const VERDICT_TONES: Readonly<Record<VerdictLevel, 'positive' | 'warn' | 'negative'>> = {
  worth: 'positive',
  promising: 'warn',
  not_yet: 'negative',
};

export interface Verdict {
  level: VerdictLevel;
  label: string;
  tone: 'positive' | 'warn' | 'negative';
  /** Why, in plain sentences, the most important first. */
  reasons: string[];
}

/** Its trial (test book) result, when it has one. */
export interface TrialResult {
  total_return: number | null;
  max_drawdown: number | null;
  days: number;
}

export interface VerdictInput {
  status: StrategyStatus;
  /** The go-live check, when it ran (not for a retired strategy). */
  golive: GoLiveReport | null;
  trial?: TrialResult | null;
  /** Robustness tests passed and run, when known. */
  tests?: { passed: number; total: number } | null;
  /** The go-live verdict alone, when the full report is not at hand (Leaderboard). */
  golivePassed?: boolean | null;
}

/** Checks whose failure says the strategy did badly, not just that it is young. */
const BAD_SIGNS = new Set<GoLiveCheckView['name']>([
  'max_drawdown',
  'max_drift',
  'survival',
  'within_mc_band',
  'quit_rule',
]);

function count(v: number | null | undefined): string {
  return formatNumber(v, { digits: 0 });
}

function pct(v: number | null | undefined, signed = false): string {
  return formatPercent(v === 0 ? 0 : v, { signed });
}

/** Both figures are known, so a sentence can quote them. */
function measured(c: GoLiveCheckView): boolean {
  return c.value != null && c.limit != null;
}

/**
 * A failing check that shows the strategy did badly. One without figures
 * only says there is no data yet, so it counts as "needs more data".
 */
export function isBadSign(c: GoLiveCheckView): boolean {
  return !c.passed && BAD_SIGNS.has(c.name) && measured(c);
}

/** Why one failing check matters, in one plain sentence. */
export function failReason(c: GoLiveCheckView): string {
  const known = measured(c);
  switch (c.name) {
    case 'status':
      return 'It is not on trial, so it has no trial record.';
    case 'min_days':
      return known
        ? `It has ${count(c.value)} of the ${count(c.limit)} trial days it needs.`
        : 'It has no trial days yet.';
    case 'min_trades':
      return known
        ? `It made ${count(c.value)} of the ${count(c.limit)} trial trades it needs.`
        : 'It has made no trial trades yet.';
    case 'max_drawdown':
      return known
        ? `Its worst drop on trial, ${pct(c.value)}, is deeper than the ${pct(c.limit)} allowed.`
        : 'Its worst drop on trial is not known yet.';
    case 'max_drift':
      return known
        ? `Its trial return is ${pct(c.value, true)} away from what its backtest expects, more than the ${pct(c.limit)} allowed.`
        : 'Its trial return cannot be compared with its backtest yet.';
    case 'survival':
      return known
        ? `It passed ${count(c.value)} of ${count(c.limit)} robustness tests. It needs to pass all of them.`
        : 'No robustness tests are on record.';
    case 'within_mc_band':
      return known
        ? 'Its drop on trial is worse than its backtest says is likely.'
        : 'There is not enough trial data to compare its drops with its backtest.';
    case 'quit_rule':
      return known
        ? 'Its drop on trial reached the point where the plan says to stop it.'
        : 'There is not enough trial data to check its drops against the plan.';
    case 'promotion_preset':
      return 'The full robustness tests have not run yet.';
    case 'nonzero_costs':
      return 'Its backtest did not count trading costs, so its results look better than they are.';
    case 'hypothesis_recorded':
      return 'Nobody has written down why it should work.';
    case 'backtest_min_trades':
      return known
        ? `Its backtest closed ${count(c.value)} trades, fewer than the ${count(c.limit)} needed to trust it.`
        : 'Its backtest has no closed trades on record.';
  }
  return c.detail;
}

function trialLine(t: TrialResult): string {
  const days = `${t.days} ${t.days === 1 ? 'day' : 'days'}`;
  const ret = t.total_return ?? 0;
  const made = ret >= 0 ? `made ${pct(ret, true)}` : `lost ${pct(Math.abs(ret))}`;
  const drop = t.max_drawdown != null ? `, with a worst drop of ${pct(t.max_drawdown)}` : '';
  return `On trial it ${made} over ${days}${drop}.`;
}

function verdict(level: VerdictLevel, reasons: string[]): Verdict {
  return { level, label: VERDICT_WORDS[level], tone: VERDICT_TONES[level], reasons };
}

/**
 * The verdict: Not good enough yet when a check shows a bad sign (a deep
 * drop, drift from its backtest, a failed robustness test), Worth following
 * when the go-live check passed and it has not lost money on trial, and
 * Promising, needs more data otherwise (too few days or trades, tests still
 * to run).
 */
export function strategyVerdict(input: VerdictInput): Verdict {
  const trial = input.trial && input.trial.days > 0 ? input.trial : null;
  const lost = trial != null && (trial.total_return ?? 0) < 0;
  const g = input.golive;

  if (g) {
    const failing = g.checks.filter((c) => !c.passed && c.name !== 'status');
    const bad = failing.filter(isBadSign);
    const young = failing.filter((c) => !isBadSign(c));
    if (bad.length) {
      return verdict('not_yet', [
        ...bad.map(failReason),
        ...(trial ? [trialLine(trial)] : []),
        ...young.map(failReason),
      ]);
    }
    if (g.passed && !lost) {
      return verdict('worth', [
        `It passed all ${g.checks.length} checks of the go-live check.`,
        ...(trial ? [trialLine(trial)] : []),
      ]);
    }
    const reasons = young.map(failReason);
    if (lost && trial) reasons.unshift(`${trialLine(trial)} Give it more time.`);
    else if (trial) reasons.push(trialLine(trial));
    if (!reasons.length) reasons.push('It needs a longer trial before anyone can judge it.');
    return verdict('promising', reasons);
  }

  const tests = input.tests;
  const failedTests = tests && tests.total > 0 ? tests.total - tests.passed : 0;
  if (failedTests > 0) {
    return verdict('not_yet', [
      `It failed ${failedTests} of ${tests!.total} robustness tests.`,
      ...(trial ? [trialLine(trial)] : []),
    ]);
  }
  if (input.golivePassed === true && !lost) {
    return verdict('worth', ['It passed the go-live check.', ...(trial ? [trialLine(trial)] : [])]);
  }
  const reasons: string[] = [];
  if (trial) reasons.push(lost ? `${trialLine(trial)} Give it more time.` : trialLine(trial));
  else reasons.push('It has no trial record yet.');
  if (tests && tests.total > 0) reasons.push(`It passed all ${tests.total} robustness tests.`);
  else reasons.push('No robustness tests are on record.');
  if (input.golivePassed === false) reasons.push('It has not passed the go-live check yet.');
  return verdict('promising', reasons);
}

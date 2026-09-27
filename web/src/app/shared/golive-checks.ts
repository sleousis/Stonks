import type { GoLiveCheckView, GoLiveReport, PromotionChecklistView } from '../api/models';
import { MISSING, formatNumber, formatPercent } from '../core/format/format';
import { toTraderWords } from './governance-labels';

export type GoLiveCheckName = GoLiveCheckView['name'];

/** Plain names for the gate's checks (src/stonks/production/golive.py). */
export const CHECK_LABELS: Record<GoLiveCheckName, string> = {
  status: 'Status',
  min_days: 'Paper days',
  max_drawdown: 'Paper drawdown',
  max_drift: 'Drift from backtest',
  min_trades: 'Paper trades',
  survival: 'Robustness tests',
  within_mc_band: 'Inside Monte Carlo band',
  quit_rule: 'Quit rule',
  promotion_preset: 'Full test suite',
  nonzero_costs: 'Realistic costs',
  hypothesis_recorded: 'Hypothesis',
  backtest_min_trades: 'Backtest trades',
};

/** What each check measures, in one sentence. */
export const CHECK_MEASURES: Record<GoLiveCheckName, string> = {
  status: 'The strategy is paper trading or live, so it has a paper period.',
  min_days: 'Distinct days with a paper snapshot.',
  max_drawdown: 'Deepest peak-to-trough fall during the paper period.',
  max_drift: 'Gap between the paper return and the return its out-of-sample backtest expects.',
  min_trades: 'Filled trades during the paper period.',
  survival: 'Every stored robustness test passed.',
  within_mc_band: 'Paper drawdown stays inside the Monte Carlo band from the backtest.',
  quit_rule: 'Drawdown stays below 1.5x the backtest worst or the Monte Carlo limit.',
  promotion_preset: 'The full robustness test suite has run.',
  nonzero_costs: 'The backtest used realistic, non-zero trading costs.',
  hypothesis_recorded: 'A written hypothesis explains why the strategy should work.',
  backtest_min_trades: 'The backtest closed enough trades to trust its statistics.',
};

export interface CheckRow {
  name: GoLiveCheckName;
  label: string;
  passed: boolean;
  measures: string;
  value: string;
  limit: string;
  detail: string;
  /** How to fix it when it fails (needs the strategy id). */
  fix: CheckFix | null;
}

function count(v: number | null | undefined): string {
  return formatNumber(v, { digits: 0 });
}

function atLeast(v: number | null, unit = ''): string {
  return v == null ? MISSING : `≥ ${count(v)}${unit}`;
}

/** A drawdown the gate reports as -0 for a flat period: show 0.00%. */
function drawdown(v: number | null): string {
  return formatPercent(v === 0 ? 0 : v);
}

/** A check's value and limit as the gate compares them. */
export function checkRow(c: GoLiveCheckView, strategyId?: string): CheckRow {
  let value = MISSING;
  let limit = MISSING;
  switch (c.name) {
    case 'min_days':
    case 'min_trades':
    case 'backtest_min_trades':
      value = count(c.value);
      limit = atLeast(c.limit);
      break;
    case 'max_drawdown':
    case 'within_mc_band':
    case 'quit_rule':
      value = drawdown(c.value);
      limit = c.limit == null ? MISSING : `≤ ${formatPercent(c.limit)}`;
      break;
    case 'max_drift':
      value = formatPercent(c.value, { signed: true });
      limit = c.limit == null ? MISSING : `± ${formatPercent(c.limit)}`;
      break;
    case 'survival':
    case 'promotion_preset':
      value =
        c.value == null || c.limit == null ? MISSING : `${count(c.value)} of ${count(c.limit)}`;
      limit = c.name === 'survival' ? 'all passed' : 'all on record';
      break;
    case 'nonzero_costs':
      value = c.value == null ? MISSING : `${count(c.value)} non-zero`;
      limit = atLeast(c.limit, ' non-zero');
      break;
    case 'hypothesis_recorded':
      value = c.value == null ? MISSING : `${count(c.value)} chars`;
      limit = atLeast(c.limit, ' chars');
      break;
    case 'status':
      break;
  }
  return {
    name: c.name,
    label: CHECK_LABELS[c.name] ?? c.name,
    passed: c.passed,
    measures: CHECK_MEASURES[c.name] ?? '',
    value,
    limit,
    detail: toTraderWords(c.detail),
    fix: strategyId ? checkFix(c, strategyId) : null,
  };
}

/** How to fix a failing check (UX-28): a link to the tool, or how long to wait. */
export interface CheckFix {
  text: string;
  link?: { label: string; commands: string[]; query?: Record<string, string> };
}

/** Lab with the strategy and the full test suite picked. */
function labFix(strategyId: string, text: string, label: string): CheckFix {
  return {
    text,
    link: { label, commands: ['/lab'], query: { strategy: strategyId, preset: 'promotion' } },
  };
}

function plural(n: number, one: string, many: string): string {
  return `${n} ${n === 1 ? one : many}`;
}

/** The next step for a failing check, or null when it passes or only time and care help. */
export function checkFix(c: GoLiveCheckView, strategyId: string): CheckFix | null {
  if (c.passed) return null;
  const short = c.value != null && c.limit != null ? Math.ceil(c.limit - c.value) : null;
  switch (c.name) {
    case 'status':
      return { text: 'Start paper trading it first.' };
    case 'min_days':
      return short && short > 0
        ? { text: `About ${plural(short, 'more trading day', 'more trading days')} on paper.` }
        : { text: 'Keep it paper trading for a few more trading days.' };
    case 'min_trades':
      return short && short > 0
        ? {
            text: `${plural(short, 'more paper trade', 'more paper trades')} needed. Keep it paper trading.`,
          }
        : { text: 'Keep it paper trading until it trades more.' };
    case 'max_drawdown':
    case 'within_mc_band':
    case 'quit_rule':
    case 'max_drift':
      return { text: 'Keep it paper trading and watch it, or stop it if it keeps falling behind.' };
    case 'survival':
    case 'promotion_preset':
      return labFix(strategyId, 'Run the full test suite in Lab.', 'Run the full tests in Lab');
    case 'nonzero_costs':
      return labFix(
        strategyId,
        'Test it again in Lab with trading costs on.',
        'Test with costs in Lab',
      );
    case 'backtest_min_trades':
      return labFix(
        strategyId,
        'Test it in Lab over a longer window so it closes more trades.',
        'Test again in Lab',
      );
    case 'hypothesis_recorded':
      return labFix(
        strategyId,
        'Write why it should work when you run it in Lab.',
        'Add it in Lab',
      );
  }
  return null;
}

export function failingChecks(report: GoLiveReport): CheckRow[] {
  return report.checks.filter((c) => !c.passed).map((c) => checkRow(c, report.strategy_id));
}

export interface ChecklistItem {
  key: keyof PromotionChecklistView;
  label: string;
  /** Glossary term for the help tip. */
  help: string | null;
  value: string;
  /** Long text (hypothesis, premortem) renders as a paragraph. */
  text: boolean;
  recorded: boolean;
}

/**
 * The promotion checklist a reviewer reads before promoting (it never
 * changes the verdict). Nothing recorded shows as "Not recorded".
 */
export function checklistItems(c: PromotionChecklistView | undefined): ChecklistItem[] {
  const list = c ?? {};
  const item = (
    key: keyof PromotionChecklistView,
    label: string,
    help: string | null,
    format: (v: number) => string,
  ): ChecklistItem => {
    const v = list[key];
    const recorded = typeof v === 'number' && Number.isFinite(v);
    return { key, label, help, value: recorded ? format(v) : 'n/a', text: false, recorded };
  };
  const text = (key: 'hypothesis' | 'premortem', label: string): ChecklistItem => {
    const v = (list[key] ?? '').trim();
    return { key, label, help: key, value: v || 'Not recorded', text: true, recorded: !!v };
  };
  return [
    item('n_trials_class', 'Trials of this class', 'trials_class', (v) => count(v)),
    item('dsr', 'Deflated Sharpe', 'deflated_sharpe', (v) => formatNumber(v, { digits: 3 })),
    item('pbo', 'PBO', 'pbo', (v) => formatNumber(v, { digits: 3 })),
    item('excess_cagr', 'Excess CAGR', 'excess_cagr', (v) => formatPercent(v, { signed: true })),
    text('hypothesis', 'Hypothesis'),
    text('premortem', 'Premortem'),
  ];
}

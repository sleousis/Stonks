import type { TickSummary } from '../../api/models';
import { strategyDisplayName } from '../../shared/strategy-names';

export interface TickOutcome {
  kind: 'winner' | 'exit' | 'error' | 'none';
  /** One line for tables and cards. */
  text: string;
  strategyId: string | null;
  /** The strategy's display name (never the raw id), or null. */
  strategyName: string | null;
}

/**
 * What a tick decided, from its summary: the winning strategy, or the
 * strategy whose positions it exited when no candidate qualified.
 */
export function tickOutcome(summary: TickSummary | null | undefined): TickOutcome {
  if (!summary) return { kind: 'none', text: '–', strategyId: null, strategyName: null };
  if (summary.error) {
    return {
      kind: 'error',
      text: summary.error_type ?? 'Error',
      strategyId: null,
      strategyName: null,
    };
  }
  const winner = summary.winner_strategy_id;
  if (winner) {
    const name = strategyDisplayName(winner, { name: summary.winner_strategy_name });
    return { kind: 'winner', text: name, strategyId: winner, strategyName: name };
  }
  const exit = summary.exit_strategy_id;
  if (exit) {
    const name = strategyDisplayName(exit, { name: summary.exit_strategy_name });
    return { kind: 'exit', text: `Exit ${name}`, strategyId: exit, strategyName: name };
  }
  return {
    kind: 'none',
    text: summary.reason ? humanize(summary.reason) : 'No winner',
    strategyId: null,
    strategyName: null,
  };
}

/** "no_candidates" → "No candidates". */
export function humanize(value: string): string {
  const text = value.replace(/_/g, ' ').trim();
  return text ? text[0].toUpperCase() + text.slice(1) : text;
}

/** Stale buys dropped / open-order conflicts, when the summary reports them. */
export function tickNotes(summary: TickSummary | null | undefined): string[] {
  if (!summary) return [];
  const notes: string[] = [];
  const stale = summary.stale_buys_dropped ?? [];
  if (stale.length) {
    notes.push(`Dropped buys on stale prices: ${stale.join(', ')}.`);
  }
  const conflicts = summary['open_order_conflicts'];
  if (Array.isArray(conflicts) && conflicts.length) {
    notes.push(`Skipped tickers with open orders at the broker: ${conflicts.join(', ')}.`);
  }
  return notes;
}

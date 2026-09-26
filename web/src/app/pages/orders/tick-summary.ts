import type { TickSummary } from '../../api/models';

export interface TickOutcome {
  kind: 'winner' | 'exit' | 'error' | 'none';
  /** One line for tables and cards. */
  text: string;
  strategyId: string | null;
}

/**
 * What a tick decided, from its summary: the winning strategy, or the
 * strategy whose positions it exited when no candidate qualified.
 */
export function tickOutcome(summary: TickSummary | null | undefined): TickOutcome {
  if (!summary) return { kind: 'none', text: '–', strategyId: null };
  if (summary.error) {
    return { kind: 'error', text: summary.error_type ?? 'Error', strategyId: null };
  }
  if (summary.winner_strategy_id) {
    return {
      kind: 'winner',
      text: summary.winner_strategy_id,
      strategyId: summary.winner_strategy_id,
    };
  }
  const exit = typeof summary['exit_strategy_id'] === 'string' ? summary['exit_strategy_id'] : null;
  if (exit) return { kind: 'exit', text: `Exit ${exit}`, strategyId: exit };
  return {
    kind: 'none',
    text: summary.reason ? humanize(summary.reason) : 'No winner',
    strategyId: null,
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
  const stale = summary['stale_buys_dropped'];
  if (Array.isArray(stale) && stale.length) {
    notes.push(`Dropped buys on stale prices: ${stale.join(', ')}.`);
  } else if (typeof stale === 'number' && stale > 0) {
    notes.push(`Dropped ${stale} buy${stale === 1 ? '' : 's'} on stale prices.`);
  }
  const conflicts = summary['open_order_conflicts'];
  if (Array.isArray(conflicts) && conflicts.length) {
    notes.push(`Skipped tickers with open orders at the broker: ${conflicts.join(', ')}.`);
  }
  return notes;
}

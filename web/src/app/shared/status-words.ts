import type { TickRun } from '../api/models';

/**
 * The one word per state from docs/design/vocabulary.md, for a trading
 * run (M5): the status key the pill draws its shape and tone from, and the
 * word people read. Pages pass both: `[status]="w.status" [label]="w.label"`.
 */
export const RUN_WORDS: Readonly<Record<TickRun['status'], { status: string; label: string }>> = {
  running: { status: 'running', label: 'Running' },
  ok: { status: 'ok', label: 'Done' },
  partial: { status: 'partial', label: 'Partly done' },
  error: { status: 'failed', label: 'Failed' },
};

/** The words for a run status, with a plain fallback for one this console does not know. */
export function runWords(status: string): { status: string; label: string } {
  const known = (RUN_WORDS as Readonly<Record<string, { status: string; label: string }>>)[status];
  return known ?? { status, label: status.charAt(0).toUpperCase() + status.slice(1) };
}

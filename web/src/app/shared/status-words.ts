import type { TickRun } from '../api/models';
import { runWord } from '../core/schedule/run-status';

/**
 * The one word per state from docs/design/vocabulary.md, for a trading
 * run (M5): the status key the pill draws its shape and tone from, and the
 * word people read. Pages pass both: `[status]="w.status" [label]="w.label"`.
 * The words themselves come from `runWord()`, the one map for every run.
 */
export const RUN_WORDS: Readonly<Record<TickRun['status'], { status: string; label: string }>> = {
  running: { status: 'running', label: runWord('running') },
  ok: { status: 'ok', label: runWord('ok') },
  partial: { status: 'partial', label: runWord('partial') },
  error: { status: 'failed', label: runWord('error') },
};

/** The words for a run status, with a plain fallback for one this console does not know. */
export function runWords(status: string): { status: string; label: string } {
  const known = (RUN_WORDS as Readonly<Record<string, { status: string; label: string }>>)[status];
  return known ?? { status, label: runWord(status) };
}

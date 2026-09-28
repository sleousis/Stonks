import type { LedgerRunView } from '../../api/models';

export type Robustness = LedgerRunView['robustness'];

/**
 * A lab run's status in the Lab's words (the API's `robustness`): did the
 * strategy hold up in its robustness tests. Independent of the trial counts,
 * so a run with many failed trials can still hold up. `status` is the pill's
 * shape and tone, `label` what people read.
 */
export const ROBUSTNESS_WORDS: Readonly<Record<Robustness, { status: string; label: string }>> = {
  running: { status: 'running', label: 'Running' },
  survived: { status: 'passed', label: 'Held up' },
  did_not_survive: { status: 'failed', label: 'Did not hold up' },
  error: { status: 'error', label: 'Failed' },
  stopped: { status: 'cancelled', label: 'Stopped' },
};

/** The words for a run's robustness, with a plain fallback for an unknown value. */
export function robustnessWords(value: string | null | undefined): { status: string; label: string } {
  const known = (ROBUSTNESS_WORDS as Readonly<Record<string, { status: string; label: string }>>)[
    value ?? ''
  ];
  return known ?? { status: value ?? 'unknown', label: value ? value.replace(/_/g, ' ') : 'Unknown' };
}

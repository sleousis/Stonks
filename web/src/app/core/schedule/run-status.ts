/**
 * One word per state for runs and checks (docs/design/vocabulary.md, Status
 * words). Trading runs, data updates, scheduled jobs and health checks come
 * back with different codes (`ok`, `succeeded`, `partial`, `error`...); the
 * admin pages show them through here so a finished run always reads "Done".
 *
 *   <app-status-pill [status]="r.status" [label]="runWord(r.status)" />
 */

const RUN_WORDS: Readonly<Record<string, string>> = {
  ok: 'Done',
  succeeded: 'Done',
  success: 'Done',
  completed: 'Done',
  partial: 'Partly done',
  failed: 'Failed',
  error: 'Failed',
  running: 'Running',
  queued: 'Waiting',
  pending: 'Waiting',
  skipped: 'Skipped',
  cancelled: 'Cancelled',
  canceled: 'Cancelled',
};

/** "Done", "Partly done", "Failed", "Running", "Skipped"... for a run's status code. */
export function runWord(status: string | null | undefined): string {
  if (!status) return 'Not run yet';
  const key = status.toLowerCase();
  return RUN_WORDS[key] ?? key.charAt(0).toUpperCase() + key.slice(1).replace(/_/g, ' ');
}

/** A health or readiness check's outcome. */
export type CheckOutcome = 'passed' | 'failed' | 'no_data';

export const CHECK_WORDS: Readonly<Record<CheckOutcome, string>> = {
  passed: 'Passed',
  failed: 'Failed',
  no_data: 'Not enough data yet',
};

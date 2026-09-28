import type { ScheduledJobView } from '../../api/models';
import { humanize } from '../../shared/ui/param-form/param-spec';

/**
 * Trader words for a scheduled job's action (UX-08). The strip, Today, the
 * command palette and the Schedule page all name jobs through here, so the
 * trading run is never called "Tick".
 */
export const JOB_LABELS: Readonly<Record<string, string>> = {
  tick: 'Trading run',
  ingest_prices: 'Price update',
  ingest_metadata: 'Company data update',
  universes_refresh: 'Universe update',
  connections_sync: 'Broker sync',
  health: 'Health check',
  report: 'Daily report',
  backup: 'Backup',
  broker_health: 'Broker gateway check',
  ibkr_reauth_reminder: 'Broker sign-in reminder',
  ingest_borrow: 'Borrow rates update',
  price_alerts: 'Price alerts',
  model_retrain: 'Model retraining',
  live_reconcile: 'Broker check',
  calendars_refresh: 'Calendar update',
  live_submit: 'Send approved orders',
  live_stops: 'Protective stops',
  options_live: 'Options expiries and rolls',
  live_gate_days: 'Stage progress',
  live_margin: 'Margin check',
  engine_start: 'Intraday engine start',
  engine_stop: 'Intraday engine stop',
};

/**
 * Words for default jobs that share an action, so the two broker checks
 * and the two options jobs never read alike.
 */
export const JOB_NAME_LABELS: Readonly<Record<string, string>> = {
  live_sod_check: 'Broker check before the open',
  live_eod_check: 'Broker check after the close',
  options_expiry_watch: 'Options expiry watch',
};

/** The action that places orders. */
export const TRADING_RUN_ACTION = 'tick';

/**
 * A default job's own words, else "Trading run" for a tick job or the
 * action's label, else the job name humanized. Never an id.
 */
export function jobLabel(job: Pick<ScheduledJobView, 'action' | 'name'>): string {
  return JOB_NAME_LABELS[job.name] ?? JOB_LABELS[job.action] ?? humanize(job.name);
}

/**
 * The job that fires next, from `GET /api/schedule`. `only` keeps jobs of
 * one action (the strip passes the trading run). A job due up to a minute
 * ago still counts, so "now" shows while it starts.
 */
export function nextJob(
  jobs: readonly ScheduledJobView[],
  now: number,
  only?: string,
): ScheduledJobView | null {
  let best: ScheduledJobView | null = null;
  let bestAt = Infinity;
  for (const j of jobs) {
    if (only && j.action !== only) continue;
    const at = j.next_run_at ? Date.parse(j.next_run_at) : NaN;
    if (Number.isFinite(at) && at >= now - 60_000 && at < bestAt) {
      best = j;
      bestAt = at;
    }
  }
  return best;
}

/** The next trading run (the `tick` action), or null when none is scheduled. */
export function nextTradingRun(
  jobs: readonly ScheduledJobView[],
  now: number,
): ScheduledJobView | null {
  return nextJob(jobs, now, TRADING_RUN_ACTION);
}

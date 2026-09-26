import type { PillTone } from '../../shared/ui/status-pill';

/** How current a (ticker, interval) series is, from the age of its last bar. */
export type Freshness = 'fresh' | 'stale' | 'old';

export interface FreshnessLimits {
  /** At most this many days old: fresh. */
  freshDays: number;
  /** Older than fresh but at most this many days: stale. Beyond: old. */
  staleDays: number;
}

const DAY_MS = 86_400_000;

/**
 * Daily and intraday series allow a long weekend before they count as
 * stale (markets close); weekly bars get about a week and a half.
 */
export function freshnessLimits(interval: string): FreshnessLimits {
  if (interval === '1w') return { freshDays: 10, staleDays: 21 };
  return { freshDays: 4, staleDays: 10 };
}

export function freshnessOf(
  lastBar: string | null | undefined,
  interval: string,
  now: Date = new Date(),
): Freshness {
  const t = lastBar ? Date.parse(lastBar) : NaN;
  if (Number.isNaN(t)) return 'old';
  const ageDays = (now.getTime() - t) / DAY_MS;
  const { freshDays, staleDays } = freshnessLimits(interval);
  if (ageDays <= freshDays) return 'fresh';
  if (ageDays <= staleDays) return 'stale';
  return 'old';
}

export const FRESHNESS_TONE: Record<Freshness, PillTone> = {
  fresh: 'positive',
  stale: 'warn',
  old: 'negative',
};

export const FRESHNESS_LABEL: Record<Freshness, string> = {
  fresh: 'Fresh',
  stale: 'Stale',
  old: 'Old',
};

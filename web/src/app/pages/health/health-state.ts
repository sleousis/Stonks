import type { HealthCheckView } from '../../api/models';
import type { PillTone } from '../../shared/ui/status-pill';

/**
 * Three states for the trader:
 * - good: the check passes;
 * - warning: needs a look soon (data a little stale, a recent ingest failure);
 * - critical: act now (no data, data far past its limit, a stuck run, a check
 *   that could not even run).
 */
export type HealthLevel = 'good' | 'warning' | 'critical';

const RANK: Record<HealthLevel, number> = { good: 0, warning: 1, critical: 2 };

export const LEVEL_TONE: Record<HealthLevel, PillTone> = {
  good: 'positive',
  warning: 'warn',
  critical: 'negative',
};

export const LEVEL_LABEL: Record<HealthLevel, string> = {
  good: 'Good',
  warning: 'Warning',
  critical: 'Critical',
};

const FRESHNESS_PREFIX = 'freshness:';
/** "latest bar 2026-09-19 (7d old, max 4d)" */
const AGE_RE = /\((\d+)d old, max (\d+)d\)/;

export function checkLevel(check: HealthCheckView): HealthLevel {
  if (check.ok) return 'good';
  if (check.detail.startsWith('check error')) return 'critical';
  if (check.name.startsWith(FRESHNESS_PREFIX)) {
    const m = AGE_RE.exec(check.detail);
    if (!m) return 'critical';
    const [age, max] = [Number(m[1]), Number(m[2])];
    return age > 2 * Math.max(max, 1) ? 'critical' : 'warning';
  }
  if (check.name === 'ingest_failures') return 'warning';
  return 'critical';
}

export function worstLevel(levels: Iterable<HealthLevel>): HealthLevel {
  let worst: HealthLevel = 'good';
  for (const level of levels) if (RANK[level] > RANK[worst]) worst = level;
  return worst;
}

export function overallLevel(checks: readonly HealthCheckView[]): HealthLevel {
  return worstLevel(checks.map(checkLevel));
}

export interface FreshnessRow {
  ticker: string;
  level: HealthLevel;
  detail: string;
}

export interface LeveledCheck extends HealthCheckView {
  level: HealthLevel;
}

/**
 * Per-ticker freshness rows (worst first, then by ticker) and the remaining
 * checks (stuck runs, failures, crashed checks) sorted by name.
 */
export function splitChecks(checks: readonly HealthCheckView[]): {
  freshness: FreshnessRow[];
  other: LeveledCheck[];
} {
  const freshness: FreshnessRow[] = [];
  const other: LeveledCheck[] = [];
  for (const c of checks) {
    const level = checkLevel(c);
    if (c.name.startsWith(FRESHNESS_PREFIX)) {
      freshness.push({ ticker: c.name.slice(FRESHNESS_PREFIX.length), level, detail: c.detail });
    } else {
      other.push({ ...c, level });
    }
  }
  freshness.sort((a, b) => RANK[b.level] - RANK[a.level] || a.ticker.localeCompare(b.ticker));
  other.sort((a, b) => a.name.localeCompare(b.name));
  return { freshness, other };
}

/** Human names for the run checks. */
export const CHECK_TITLES: Record<string, string> = {
  stuck_ticks: 'Stuck ticks',
  stuck_ingest_runs: 'Stuck ingest runs',
  ingest_failures: 'Recent ingest failures',
  freshness: 'Freshness check',
};

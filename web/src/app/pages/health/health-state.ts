import type { HealthCheckView, HealthConfig } from '../../api/models';
import { CHECK_WORDS, type CheckOutcome } from '../../core/schedule/run-status';
import { humanize } from '../../shared/ui/param-form/param-spec';
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

/** The pill on one check: the vocabulary's check words, the tone tells how urgent. */
export const LEVEL_LABEL: Record<HealthLevel, string> = {
  good: CHECK_WORDS.passed,
  warning: CHECK_WORDS.failed,
  critical: CHECK_WORDS.failed,
};

/** How urgent the overall state is, beside the count of failed checks. */
export const LEVEL_URGENCY: Record<HealthLevel, string> = {
  good: 'All good',
  warning: 'Needs a look soon',
  critical: 'Act now',
};

const FRESHNESS_PREFIX = 'freshness:';
/** One check per broker gateway; the broker gateways panel shows them. */
const BROKER_PREFIX = 'broker:';
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
 * checks (stuck runs, failures, crashed checks) sorted by name. Broker
 * gateway checks still count toward the overall level, but show in their
 * own panel.
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
      freshness.push({
        ticker: c.name.slice(FRESHNESS_PREFIX.length),
        level,
        detail: plainDetail(c),
      });
    } else if (!c.name.startsWith(BROKER_PREFIX)) {
      other.push({ ...c, level });
    }
  }
  freshness.sort((a, b) => RANK[b.level] - RANK[a.level] || a.ticker.localeCompare(b.ticker));
  // Failing checks first, then by the name people read.
  other.sort(
    (a, b) => RANK[b.level] - RANK[a.level] || checkTitle(a.name).localeCompare(checkTitle(b.name)),
  );
  return { freshness, other };
}

/**
 * The limit a check compares against, in words, from the report's
 * `[production.health]` thresholds; `null` when the API did not send it.
 */
export function checkThreshold(name: string, t: HealthConfig | null | undefined): string | null {
  if (!t) return null;
  const n = (v: number | undefined, unit: string) => (v == null ? null : `${v} ${unit}`);
  switch (name) {
    case 'stuck_ticks': {
      const v = n(t.stuck_tick_minutes, 'min');
      return v && `Stuck when running over ${v}`;
    }
    case 'stuck_ingest_runs': {
      const v = n(t.stuck_ingest_minutes, 'min');
      return v && `Stuck when running over ${v}`;
    }
    case 'ingest_failures': {
      const v = n(t.ingest_failure_lookback_hours, 'h');
      return v && `Failures in the last ${v}`;
    }
    case 'freshness': {
      const days = t.max_bar_age_days;
      return days == null ? null : `Latest price at most ${days} day${days === 1 ? '' : 's'} old`;
    }
    default:
      return name.startsWith(FRESHNESS_PREFIX) ? checkThreshold('freshness', t) : null;
  }
}

/** A check's name and one line on what it watches, for people. */
export interface CheckInfo {
  title: string;
  meaning: string;
}

/**
 * Every check the server runs, by its id. The Health page and the Dashboard
 * show these, never the id ("lab_queue"). A new server check needs an entry
 * here; until then its id is written out in words.
 */
export const CHECKS: Readonly<Record<string, CheckInfo>> = {
  freshness: {
    title: 'Price data is up to date',
    meaning: 'Each traded ticker has a recent daily price, so trading runs decide on current data.',
  },
  stuck_ticks: {
    title: 'Stuck trading runs',
    meaning: 'A trading run that runs far longer than usual has probably hung.',
  },
  stuck_ingest_runs: {
    title: 'Stuck data updates',
    meaning: 'A data update that never finishes holds up the next one.',
  },
  ingest_failures: {
    title: 'Recent data update failures',
    meaning: 'Data updates that ended in an error lately.',
  },
  var_violations: {
    title: 'Risk estimate accuracy',
    meaning:
      'Counts the days a portfolio lost more than its daily risk estimate. Far too many or too few means the estimate is off.',
  },
  lab_queue: {
    title: 'Lab workers',
    meaning: 'Lab jobs sent to separate workers get picked up and finish.',
  },
  risk_halts: {
    title: 'Trading stops',
    meaning: 'Whether a stop on trading is in force: Stop trading, a loss limit or a data problem.',
  },
};

/** Human names for the run checks (kept for older callers). */
export const CHECK_TITLES: Record<string, string> = Object.fromEntries(
  Object.entries(CHECKS).map(([id, info]) => [id, info.title]),
);

/** "Stuck trading runs", "Freshness of AAPL.US"; never the raw id. */
export function checkTitle(name: string): string {
  if (CHECKS[name]) return CHECKS[name].title;
  if (name.startsWith(FRESHNESS_PREFIX)) {
    return `Freshness of ${name.slice(FRESHNESS_PREFIX.length)}`;
  }
  if (name.startsWith(BROKER_PREFIX)) {
    return `Broker gateway ${name.slice(BROKER_PREFIX.length)}`;
  }
  return humanize(name.replace(/:+/g, ' '));
}

/** Where an admin acts on a failing check. */
export const CHECK_ACTIONS: Readonly<Record<string, { label: string; link: string }>> = {
  stuck_ticks: { label: 'Open trading runs', link: '/orders/ticks' },
  stuck_ingest_runs: { label: 'Open data updates', link: '/data' },
  ingest_failures: { label: 'Open data updates', link: '/data' },
  var_violations: { label: 'Open risk', link: '/insights/risk' },
  lab_queue: { label: 'Open the lab', link: '/lab' },
  risk_halts: { label: 'Open halts', link: '/ops/halts' },
};

/** One line on what the check watches, or null for a check we do not know. */
export function checkMeaning(name: string): string | null {
  const base = name.startsWith(FRESHNESS_PREFIX) ? 'freshness' : name;
  return CHECKS[base]?.meaning ?? null;
}

/** What the server says when a check passes only for lack of data. */
const NO_DATA_DETAILS = new Set(['not enough days yet']);

/** Passed, failed, or passed for lack of data ("Not enough data yet"). */
export function checkOutcome(check: HealthCheckView): CheckOutcome {
  if (!check.ok) return 'failed';
  return NO_DATA_DETAILS.has(check.detail) ? 'no_data' : 'passed';
}

/** The pill for one check: its word and tone. */
export function checkPill(check: HealthCheckView): { label: string; tone: PillTone } {
  if (checkOutcome(check) === 'no_data') return { label: CHECK_WORDS.no_data, tone: 'neutral' };
  const level = checkLevel(check);
  return { label: LEVEL_LABEL[level], tone: LEVEL_TONE[level] };
}

const plural = (n: number, one: string, many = `${one}s`) => `${n} ${n === 1 ? one : many}`;
const listCount = (text: string) => text.split(',').filter((s) => s.trim()).length;

/**
 * The server's detail in plain words: "None." for "none", counts instead of
 * run ids, "Latest price 2026-09-25, 3 days old." for a freshness row. An
 * unknown detail shows as it came, with a capital letter.
 */
export function plainDetail(check: HealthCheckView): string {
  const d = check.detail.trim();
  if (d === '' || d === 'none') return 'None.';
  if (d.startsWith('check error')) {
    const reason = d.replace(/^check error:?\s*/, '') || 'no reason given';
    return `The check could not run: ${reason}.`;
  }
  let m: RegExpExecArray | null;
  if (check.name.startsWith(FRESHNESS_PREFIX) || check.name === 'freshness') {
    if (d === 'no daily bars') return 'No daily prices stored yet.';
    if ((m = /^latest bar (\S+) \((\d+)d old/.exec(d))) {
      return `Latest price ${m[1]}, ${plural(Number(m[2]), 'day')} old.`;
    }
  }
  switch (check.name) {
    case 'stuck_ticks':
      if ((m = /^running > (\d+)m: (.*)$/.exec(d))) {
        return `${plural(listCount(m[2]), 'trading run')} running over ${m[1]} minutes.`;
      }
      break;
    case 'stuck_ingest_runs':
      if ((m = /^running > (\d+)m: (.*)$/.exec(d))) {
        return `${plural(listCount(m[2]), 'data update')} running over ${m[1]} minutes.`;
      }
      break;
    case 'ingest_failures':
      if ((m = /^failed in last (\d+)h: (.*)$/.exec(d))) {
        return `${plural(listCount(m[2]), 'data update')} failed in the last ${m[1]} hours.`;
      }
      break;
    case 'var_violations':
      if (d === 'not enough days yet') return 'Too few trading days scored to judge yet.';
      if (d === 'within band' || d === 'within band or too few days') {
        return 'Within the expected range.';
      }
      if (d === 'no risk_snapshots table') return 'No risk estimates recorded yet.';
      if ((m = /^outside [^:]+: (.*)$/.exec(d))) {
        return `${plural(listCount(m[1]), 'portfolio')} outside the expected range.`;
      }
      break;
    case 'lab_queue': {
      if (d === 'no lab queue table') return 'Lab workers are not in use.';
      const q = /(\d+) queued, (\d+) running, (\d+) worker\(s\) alive/.exec(d);
      const summary = q
        ? `${q[1]} waiting, ${q[2]} running, ${plural(Number(q[3]), 'worker')} up.`
        : '';
      if ((m = /^(\d+) running job\(s\) lost their worker/.exec(d))) {
        return `${plural(Number(m[1]), 'running job')} lost the worker. ${summary}`.trim();
      }
      if ((m = /^oldest job waited (\d+) min with no live worker/.exec(d))) {
        return `A job waited ${m[1]} minutes with no worker running. ${summary}`.trim();
      }
      if (summary) return summary;
      break;
    }
    case 'risk_halts':
      if (d === 'no halt in force' || d === 'no risk_halts table') return 'No stop in force.';
      return `${plural(d.split(';').length, 'stop')} in force.`;
  }
  return d.charAt(0).toUpperCase() + d.slice(1);
}

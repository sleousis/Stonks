import { strategyDisplayName } from '../../shared/strategy-names';
import { formatNumber, formatPercent } from '../../core/format/format';

/** An R multiple ("+2.0R"), or "n/a" when no stop is known. */
export function formatR(value: number | null | undefined): string {
  if (value === null || value === undefined || !Number.isFinite(value)) return 'n/a';
  return `${value > 0 ? '+' : ''}${value.toFixed(1)}R`;
}

/** An excursion or efficiency as a percent, or "n/a". */
export function formatShare(value: number | null | undefined, signed = false): string {
  if (value === null || value === undefined || !Number.isFinite(value)) return 'n/a';
  return formatPercent(value, { digits: 1, signed });
}

/** Holding time in plain words. */
export function formatHolding(days: number): string {
  if (!Number.isFinite(days) || days < 0) return 'n/a';
  if (days < 1) {
    const hours = Math.round(days * 24);
    return hours <= 1 ? 'Under an hour' : `${hours} hours`;
  }
  const rounded = Math.round(days * 10) / 10;
  return rounded === 1 ? '1 day' : `${formatNumber(rounded, { digits: 1 })} days`;
}

const EXITS: Record<string, string> = {
  stop: 'Stop',
  signal: 'Strategy signal',
  exit_no_pick: 'No new pick',
  risk_rule: 'Risk rule',
  manual: 'By hand',
};

/** Why a trade closed, in plain words. */
export function exitLabel(trigger: string | null | undefined, open: boolean): string {
  if (open) return 'Still open';
  if (!trigger) return 'Not recorded';
  return EXITS[trigger] ?? trigger.replace(/_/g, ' ');
}

/**
 * The sleeve's name: the strategy's display name (its title when the API
 * sends one as `sleeve_name`), or "By hand" for manual trades.
 */
export function sleeveLabel(sleeve: string, name?: string | null): string {
  if (sleeve === 'manual') return 'By hand';
  if (sleeve === 'unattributed') return 'No strategy';
  return strategyDisplayName(sleeve, { name });
}

export function planLabel(followed: boolean | null | undefined): string {
  if (followed === true) return 'Followed the plan';
  if (followed === false) return 'Broke the plan';
  return 'Not said';
}

const GROUP_KEYS: Record<string, string> = {
  followed: 'Followed the plan',
  broke: 'Broke the plan',
  not_said: 'Not said',
  untagged: 'No tag',
  none: 'None',
  manual: 'By hand',
  strategy: 'Strategy',
  long: 'Long',
  short: 'Short',
  open: 'Still open',
  all: 'All trades',
};

/** A breakdown group's key in plain words. */
export function groupLabel(key: string, by: string): string {
  if (by === 'exit_trigger') return exitLabel(key === 'open' ? null : key, key === 'open');
  if (by === 'sleeve') return sleeveLabel(key);
  return GROUP_KEYS[key] ?? key;
}

/** "gap up, earnings" as ["gap up", "earnings"]: trimmed, lower case, no repeats. */
export function parseLabels(text: string): string[] {
  const out: string[] = [];
  for (const raw of text.split(',')) {
    const label = raw.replace(/\s+/g, ' ').trim().toLowerCase();
    if (label && !out.includes(label)) out.push(label);
  }
  return out;
}

export interface DayCell {
  /** ISO day, or null for a pad cell outside the month. */
  iso: string | null;
  day: number | null;
  pnl: number | null;
  trades: number;
}

export interface WeekRow {
  cells: DayCell[];
  /** The ISO week's total over the days shown. */
  total: number | null;
  trades: number;
}

interface DayBucket {
  key: string;
  pnl: number;
  trades: number;
}

/** One month ("2026-03") as weeks of seven cells, Monday first. */
export function monthGrid(month: string, days: readonly DayBucket[]): WeekRow[] {
  const [y, m] = month.split('-').map(Number);
  const first = new Date(Date.UTC(y, m - 1, 1));
  const count = new Date(Date.UTC(y, m, 0)).getUTCDate();
  const lead = (first.getUTCDay() + 6) % 7;
  const byDay = new Map(days.map((d) => [d.key, d]));
  const cells: DayCell[] = Array.from({ length: lead }, () => ({
    iso: null,
    day: null,
    pnl: null,
    trades: 0,
  }));
  for (let d = 1; d <= count; d++) {
    const iso = `${month}-${String(d).padStart(2, '0')}`;
    const bucket = byDay.get(iso);
    cells.push({ iso, day: d, pnl: bucket?.pnl ?? null, trades: bucket?.trades ?? 0 });
  }
  while (cells.length % 7) cells.push({ iso: null, day: null, pnl: null, trades: 0 });
  const weeks: WeekRow[] = [];
  for (let i = 0; i < cells.length; i += 7) {
    const row = cells.slice(i, i + 7);
    const traded = row.filter((c) => c.pnl !== null);
    weeks.push({
      cells: row,
      total: traded.length ? traded.reduce((s, c) => s + (c.pnl ?? 0), 0) : null,
      trades: row.reduce((s, c) => s + c.trades, 0),
    });
  }
  return weeks;
}

/** The month before or after "YYYY-MM". */
export function shiftMonth(month: string, by: number): string {
  const [y, m] = month.split('-').map(Number);
  const d = new Date(Date.UTC(y, m - 1 + by, 1));
  return `${d.getUTCFullYear()}-${String(d.getUTCMonth() + 1).padStart(2, '0')}`;
}

/** "March 2026". */
export function monthName(month: string): string {
  const [y, m] = month.split('-').map(Number);
  return new Date(Date.UTC(y, m - 1, 1)).toLocaleDateString(undefined, {
    month: 'long',
    year: 'numeric',
    timeZone: 'UTC',
  });
}

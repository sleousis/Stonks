import { activeFormat, formatMoney, formatPercent } from './format';

const DAY_MS = 24 * 60 * 60 * 1000;

/**
 * The name of the last session (UX-34): "Today" for today's session, the
 * weekday ("Friday") for one earlier this week, else "Last session". `day`
 * is the P&L row's date (YYYY-MM-DD).
 */
export function sessionLabel(day: string | null | undefined, now = new Date()): string {
  if (!day) return 'Today';
  const { locale, timeZone } = activeFormat();
  const today = new Intl.DateTimeFormat('en-CA', { timeZone }).format(now);
  if (day === today) return 'Today';
  const at = Date.parse(`${day}T00:00:00Z`);
  const days = Math.round((Date.parse(`${today}T00:00:00Z`) - at) / DAY_MS);
  if (!Number.isFinite(days) || days < 0 || days > 6) return 'Last session';
  return new Intl.DateTimeFormat(locale, { weekday: 'long', timeZone: 'UTC' }).format(at);
}

/** "today", "on Friday" or "last session", to end a sentence. */
export function sessionPhrase(day: string | null | undefined, now = new Date()): string {
  const label = sessionLabel(day, now);
  if (label === 'Today') return 'today';
  if (label === 'Last session') return 'last session';
  return `on ${label}`;
}

/** The day's change in money, signed, in one format for every page (M2). */
export function dayChangeMoney(
  change: number | null | undefined,
  currency?: string | null,
): string {
  return formatMoney(change, { signed: true, currency: currency ?? undefined });
}

/** The day's change as a percent: signed, two decimals, on every page (M2). */
export function dayChangePercent(ret: number | null | undefined): string {
  return formatPercent(ret, { signed: true, digits: 2 });
}

/**
 * The day's change as one line, the same on Today, Dashboard and Insights
 * (M2): "-US$6.09 (-0.06%) on Thursday". Null when there is no change yet.
 */
export function dayChangeLine(
  change: number | null | undefined,
  ret: number | null | undefined,
  day: string | null | undefined,
  currency?: string | null,
  now = new Date(),
): string | null {
  if (change == null) return null;
  const pct = ret == null ? '' : ` (${dayChangePercent(ret)})`;
  return `${dayChangeMoney(change, currency)}${pct} ${sessionPhrase(day, now)}`;
}

/** The day's change figures a page shows: the change, its percent and the session. */
export interface DayChangeFigures {
  change: number | null;
  pct: number | null;
  day: string | null;
}

/**
 * The headline day change from the API's one `day_change` (`/api/pnl` and
 * `/api/insights` both serve it), so Today, Dashboard and Insights show the
 * same number. A server without it falls back to the latest P&L row.
 */
export function dayChangeFrom(
  view: { change: number | null; change_pct: number | null; day: string } | null | undefined,
  row?: {
    daily_change?: number | null;
    daily_return?: number | null;
    day?: string | null;
  } | null,
): DayChangeFigures | null {
  if (view) return { change: view.change, pct: view.change_pct, day: view.day };
  if (!row) return null;
  return { change: row.daily_change ?? null, pct: row.daily_return ?? null, day: row.day ?? null };
}

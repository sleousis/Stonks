import type { EarningsEvent, EconomicEvent, SentimentDay } from '../../api/models';

export type CalendarScope = 'holdings' | 'watchlists' | 'tickers' | 'all';

/** Whose events the page shows. */
export interface ScopeState {
  scope: CalendarScope;
  /** One of your watchlists, or null for all of them (scope `watchlists`). */
  watchlistId: string | null;
  /** Scope `tickers`. */
  tickers: readonly string[];
}

/** The longest window one calendar read may span (the API refuses more). */
export const MAX_WINDOW_DAYS = 120;

/** "aapl.us, msft.us\nnvda.us" to ["AAPL.US", "MSFT.US", "NVDA.US"], no repeats. */
export function parseTickers(text: string): string[] {
  const out: string[] = [];
  for (const raw of text.split(/[\s,;]+/)) {
    const t = raw.trim().toUpperCase();
    if (t && !out.includes(t)) out.push(t);
  }
  return out;
}

/** Country codes for the economic filter: two letters or a region such as EU. */
export function parseCountries(text: string): string[] {
  return parseTickers(text).filter((c) => /^[A-Z]{2,3}$/.test(c));
}

/**
 * The scope part of a calendar or news query, or null when it cannot be
 * asked yet (scope `tickers` with no tickers).
 */
export function scopeQuery(s: ScopeState): {
  scope: CalendarScope;
  watchlist_id?: string;
  tickers?: string;
} | null {
  switch (s.scope) {
    case 'watchlists':
      return s.watchlistId
        ? { scope: 'watchlists', watchlist_id: s.watchlistId }
        : { scope: 'watchlists' };
    case 'tickers':
      return s.tickers.length ? { scope: 'tickers', tickers: s.tickers.join(',') } : null;
    default:
      return { scope: s.scope };
  }
}

/** `YYYY-MM-DD` plus `days` (negative goes back). Date-only, no time zone shift. */
export function addDays(iso: string, days: number): string {
  const [y, m, d] = iso.split('-').map(Number);
  const t = new Date(Date.UTC(y, m - 1, d + days));
  return t.toISOString().slice(0, 10);
}

/** Whole days from `start` to `end` (both `YYYY-MM-DD`). */
export function daysBetween(start: string, end: string): number {
  const a = Date.parse(`${start}T00:00:00Z`);
  const b = Date.parse(`${end}T00:00:00Z`);
  return Math.round((b - a) / 86_400_000);
}

/** What is wrong with a window, or null. */
export function windowError(start: string, end: string): string | null {
  if (!start || !end) return 'Pick both dates.';
  const span = daysBetween(start, end);
  if (Number.isNaN(span)) return 'Pick both dates.';
  if (span < 0) return 'The end comes before the start.';
  if (span > MAX_WINDOW_DAYS) return `Pick at most ${MAX_WINDOW_DAYS} days.`;
  return null;
}

/** When in the day a company reports. */
export function timingLabel(value: EarningsEvent['before_after_market']): string {
  switch (value) {
    case 'before':
      return 'Before the open';
    case 'during':
      return 'During the day';
    case 'after':
      return 'After the close';
    default:
      return 'Time not given';
  }
}

/** What an economic release compares against. */
/** How much a release tends to move markets, in plain words. */
export function importanceLabel(value: EconomicEvent['importance']): string {
  switch (value) {
    case 'high':
      return 'High';
    case 'medium':
      return 'Medium';
    default:
      return 'Low';
  }
}

export function comparisonLabel(value: EconomicEvent['comparison']): string {
  switch (value) {
    case 'mom':
      return 'Month on month';
    case 'qoq':
      return 'Quarter on quarter';
    case 'yoy':
      return 'Year on year';
    default:
      return 'Level';
  }
}

/** Sentiment scores run from -1 to 1. Words, so it never rests on colour alone. */
export function sentimentWord(value: number | null | undefined): string {
  if (value === null || value === undefined || Number.isNaN(value)) return 'No score';
  if (value >= 0.15) return 'Positive';
  if (value <= -0.15) return 'Negative';
  return 'Neutral';
}

export interface SentimentSummary {
  ticker: string;
  /** The newest day's score. */
  latest: number | null;
  latestDay: string | null;
  /** Mean of the scored days, weighted by articles when counts are given. */
  average: number | null;
  articles: number;
  days: number;
}

/** One row per ticker from the daily sentiment (oldest first from the API). */
export function summarizeSentiment(days: readonly SentimentDay[]): SentimentSummary[] {
  const byTicker = new Map<string, SentimentDay[]>();
  for (const d of days) {
    const list = byTicker.get(d.ticker) ?? [];
    list.push(d);
    byTicker.set(d.ticker, list);
  }
  const out: SentimentSummary[] = [];
  for (const [ticker, list] of byTicker) {
    const sorted = [...list].sort((a, b) => a.day.localeCompare(b.day));
    const scored = sorted.filter((d) => typeof d.sentiment === 'number');
    let weight = 0;
    let total = 0;
    for (const d of scored) {
      const w = d.article_count && d.article_count > 0 ? d.article_count : 1;
      weight += w;
      total += (d.sentiment as number) * w;
    }
    const last = scored.at(-1) ?? null;
    out.push({
      ticker,
      latest: last ? (last.sentiment as number) : null,
      latestDay: last?.day ?? null,
      average: weight ? total / weight : null,
      articles: sorted.reduce((n, d) => n + (d.article_count ?? 0), 0),
      days: sorted.length,
    });
  }
  return out.sort((a, b) => a.ticker.localeCompare(b.ticker));
}

/** Only http(s) links leave the console. */
export function safeUrl(url: string | null | undefined): string | null {
  return url && /^https?:\/\//i.test(url) ? url : null;
}

import type {
  DividendEvent,
  EarningsEvent,
  EconomicEvent,
  FilingEvent,
  SentimentDay,
} from '../../api/models';
import {
  MISSING,
  activeFormat,
  formatDate,
  formatMoney,
  formatNumber,
  formatTime,
  isoDay,
} from '../../core/format/format';

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

type ReleaseKind = Pick<EconomicEvent, 'event_type' | 'comparison'>;

/** "CPI (year on year)": the release with what it compares, in one cell. */
export function economicRelease(e: ReleaseKind): string {
  if (e.comparison === 'none') return e.event_type;
  return `${e.event_type} (${comparisonLabel(e.comparison).toLowerCase()})`;
}

/**
 * The unit of a release's figures, read from what it is: changes and rates
 * are percent, payrolls are thousands of jobs, anything else (an index such
 * as a PMI) has none.
 */
function economicUnit(e: ReleaseKind): '%' | 'K jobs' | '' {
  const name = e.event_type.toLowerCase();
  if (/payroll|employment change|\bjobs\b/.test(name)) return 'K jobs';
  if (e.comparison !== 'none' || /\brate\b|inflation|\bcpi\b|\bppi\b|yield/.test(name)) return '%';
  return '';
}

/** "2.5%", "210K jobs", "52.1". */
export function economicValue(value: number | null | undefined, e: ReleaseKind): string {
  if (value === null || value === undefined || !Number.isFinite(value)) return MISSING;
  const unit = economicUnit(e);
  const text = formatNumber(value);
  if (unit === '%') return `${text}%`;
  if (unit === 'K jobs') return `${text}K jobs`;
  return text;
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

/** What a filing announces: each item's name, or "Item 5.02" when no name is known. */
export function filingItemsLabel(f: { items?: string[]; item_names?: string[] }): string {
  const items = f.items ?? [];
  const names = f.item_names ?? [];
  return items.map((code, i) => names[i]?.trim() || `Item ${code}`).join(', ');
}

/** The kinds of event on the agenda. */
export type AgendaKind = 'earnings' | 'dividend' | 'economic' | 'filing';

/** A figure on an agenda row: "Estimate $1.60". */
export interface AgendaFigure {
  label: string;
  value: string;
}

/** One event on the agenda, ready to render. */
export interface AgendaItem {
  key: string;
  kind: AgendaKind;
  /** YYYY-MM-DD in the preferred zone. */
  day: string;
  /** Sorts within the day. */
  at: string;
  /** "After close", "14:30", "Ex-date". */
  when: string;
  /** A ticker to chart, or null for an economic release. */
  ticker: string | null;
  title: string;
  meta: string;
  figures: AgendaFigure[];
  /** Economic releases: High, Medium or Low. */
  importance?: string;
  /** Filings: the document on the SEC site (http(s) only). */
  url?: string | null;
  form?: string;
}

/** A day of the agenda: its date and the events on it. */
export interface AgendaDay {
  day: string;
  items: AgendaItem[];
}

/** The chip text of each kind. */
export const KIND_LABELS: Record<AgendaKind, string> = {
  earnings: 'Earnings',
  dividend: 'Ex-dividend',
  economic: 'Economic',
  filing: 'Filing',
};

/** A short "when" for an earnings report. */
export function timingShort(value: EarningsEvent['before_after_market']): string {
  switch (value) {
    case 'before':
      return 'Before open';
    case 'during':
      return 'In session';
    case 'after':
      return 'After close';
    default:
      return 'No time';
  }
}

/** A timestamp's day in the preferred zone. */
function zoneDay(value: string): string {
  if (/^\d{4}-\d{2}-\d{2}$/.test(value)) return value;
  const d = new Date(value);
  return Number.isNaN(d.getTime()) ? value.slice(0, 10) : isoDay(d);
}

const TIMING_ORDER: Record<string, string> = { before: '0', during: '1', after: '2' };

export function earningsItem(e: EarningsEvent): AgendaItem {
  const money = (v: number | null | undefined) =>
    formatMoney(v, { currency: e.currency ?? undefined });
  const figures: AgendaFigure[] = [{ label: 'Estimate', value: money(e.eps_estimate) }];
  if (e.eps_actual != null) figures.push({ label: 'Actual', value: money(e.eps_actual) });
  if (typeof e.surprise_percent === 'number') {
    const pct = formatNumber(e.surprise_percent, { digits: 1, signed: true });
    figures.push({ label: 'Surprise', value: `${pct}%` });
  }
  const quarter = e.period_end ? `quarter ending ${formatDate(e.period_end)}` : '';
  return {
    key: `e|${e.ticker}|${e.period_end}`,
    kind: 'earnings',
    day: e.report_date,
    at: TIMING_ORDER[e.before_after_market ?? ''] ?? '3',
    when: timingShort(e.before_after_market),
    ticker: e.ticker,
    title: e.ticker,
    meta: [e.name, timingLabel(e.before_after_market), quarter].filter(Boolean).join(', '),
    figures,
  };
}

export function dividendItem(d: DividendEvent): AgendaItem {
  const figures: AgendaFigure[] = [
    { label: 'Amount', value: formatMoney(d.amount, { currency: d.currency ?? undefined }) },
  ];
  if (d.pay_date) figures.push({ label: 'Paid', value: formatDate(d.pay_date) });
  const record = d.record_date ? `record ${formatDate(d.record_date)}` : '';
  return {
    key: `d|${d.ticker}|${d.ex_date}`,
    kind: 'dividend',
    day: d.ex_date,
    at: '0',
    when: 'Ex-date',
    ticker: d.ticker,
    title: d.ticker,
    meta: [d.name, record].filter(Boolean).join(', '),
    figures,
  };
}

export function economicItem(e: EconomicEvent): AgendaItem {
  return {
    key: `x|${e.country}|${e.event_time}|${e.event_type}|${e.comparison}`,
    kind: 'economic',
    day: zoneDay(e.event_time),
    at: e.event_time,
    when: formatTime(e.event_time),
    ticker: null,
    title: economicRelease(e),
    meta: [e.country, e.period].filter(Boolean).join(', '),
    importance: importanceLabel(e.importance),
    figures: [
      { label: 'Actual', value: economicValue(e.actual, e) },
      { label: 'Estimate', value: economicValue(e.estimate, e) },
      { label: 'Previous', value: economicValue(e.previous, e) },
    ],
  };
}

export function filingItem(f: FilingEvent): AgendaItem {
  return {
    key: `f|${f.ticker}|${f.accepted_at}|${f.form}`,
    kind: 'filing',
    day: zoneDay(f.accepted_at),
    at: f.accepted_at,
    when: formatTime(f.accepted_at),
    ticker: f.ticker,
    title: f.ticker,
    meta: [f.name, filingItemsLabel(f)].filter(Boolean).join(', '),
    figures: [],
    url: safeUrl(f.url),
    form: f.form,
  };
}

/** Events grouped by day: days in order (newest first when `newestFirst`), events by time. */
export function groupByDay(items: readonly AgendaItem[], newestFirst = false): AgendaDay[] {
  const byDay = new Map<string, AgendaItem[]>();
  for (const it of items) {
    const list = byDay.get(it.day) ?? [];
    list.push(it);
    byDay.set(it.day, list);
  }
  const dir = newestFirst ? -1 : 1;
  return [...byDay.entries()]
    .sort(([a], [b]) => dir * a.localeCompare(b))
    .map(([day, list]) => ({
      day,
      items: [...list].sort(
        (a, b) => dir * a.at.localeCompare(b.at) || a.key.localeCompare(b.key),
      ),
    }));
}

/** "Mon 13 Oct": the day row of the agenda, in the preferred locale. */
export function agendaDayLabel(day: string): string {
  const d = new Date(`${day}T00:00:00Z`);
  if (Number.isNaN(d.getTime())) return day;
  const { locale } = activeFormat();
  return new Intl.DateTimeFormat(locale, {
    weekday: 'short',
    day: 'numeric',
    month: 'short',
    timeZone: 'UTC',
  }).format(d);
}

/** "Today", "Tomorrow", "Yesterday" or null, beside the day row. */
export function relativeDay(day: string, today = isoDay()): string | null {
  const span = daysBetween(today, day);
  if (span === 0) return 'Today';
  if (span === 1) return 'Tomorrow';
  if (span === -1) return 'Yesterday';
  return null;
}

// Number and date formatting for the whole console. Use these (or the pipes
// in shared/format.pipes.ts) instead of ad-hoc toFixed() calls so every page
// shows figures the same way. Missing values render as an en dash.
//
// Locale, time zone and date style come from one place: the active display
// preferences below, which FormatService (format.service.ts) sets from the
// user's choice in Settings. They are a signal, so templates and computed()
// values that format a figure re-render when the preference changes.

import { signal } from '@angular/core';

/** Used when the API sends no currency for a figure. */
export const DEFAULT_CURRENCY = 'USD';
export const MISSING = '–';

/** `iso` shows 2026-09-26 (unambiguous everywhere); `locale` follows the locale. */
export type DateStyle = 'iso' | 'locale';

/** Fully resolved preferences the formatters read. */
export interface ActiveFormat {
  /** BCP 47 tag, e.g. `en-US`, `de-DE`. */
  locale: string;
  /** IANA zone, e.g. `Europe/London`. */
  timeZone: string;
  dateStyle: DateStyle;
}

/** The browser's own locale and zone, with ISO dates: what a new user sees. */
export function browserFormat(): ActiveFormat {
  let locale = 'en-US';
  let timeZone = 'UTC';
  try {
    const resolved = new Intl.DateTimeFormat().resolvedOptions();
    locale = globalThis.navigator?.language || resolved.locale || locale;
    timeZone = resolved.timeZone || timeZone;
  } catch {
    // Keep the fallbacks.
  }
  return { locale, timeZone, dateStyle: 'iso' };
}

/** Written only by FormatService; read by every formatter below. */
export const activeFormat = signal<ActiveFormat>(browserFormat());

type Num = number | null | undefined;

export interface NumberOptions {
  /** Prefix positive values with "+" (changes, returns, P&L). */
  signed?: boolean;
  /** 1.2K / 3.4M (axes, tight tiles). */
  compact?: boolean;
  digits?: number;
  /** ISO 4217 code from the API (`PortfolioView.currency`); defaults to USD. */
  currency?: string | null;
}

// Intl formatters are costly to build; keep one per locale and option set.
const numberCache = new Map<string, Intl.NumberFormat>();
const dateCache = new Map<string, Intl.DateTimeFormat>();

function numberFormat(locale: string, options: Intl.NumberFormatOptions): Intl.NumberFormat {
  const key = `${locale}|${JSON.stringify(options)}`;
  let fmt = numberCache.get(key);
  if (!fmt) {
    fmt = new Intl.NumberFormat(locale, options);
    numberCache.set(key, fmt);
  }
  return fmt;
}

function dateFormat(locale: string, options: Intl.DateTimeFormatOptions): Intl.DateTimeFormat {
  const key = `${locale}|${JSON.stringify(options)}`;
  let fmt = dateCache.get(key);
  if (!fmt) {
    fmt = new Intl.DateTimeFormat(locale, options);
    dateCache.set(key, fmt);
  }
  return fmt;
}

function signedText(text: string, value: number, signed: boolean | undefined): string {
  return signed && value > 0 ? `+${text}` : text;
}

export function formatMoney(value: Num, opts: NumberOptions = {}): string {
  if (!isNum(value)) return MISSING;
  const { locale } = activeFormat();
  const options: Intl.NumberFormatOptions = opts.compact
    ? {
        style: 'currency',
        currency: opts.currency || DEFAULT_CURRENCY,
        notation: 'compact',
        maximumFractionDigits: 1,
      }
    : {
        style: 'currency',
        currency: opts.currency || DEFAULT_CURRENCY,
        minimumFractionDigits: 2,
        maximumFractionDigits: 2,
      };
  return signedText(numberFormat(locale, options).format(value), value, opts.signed);
}

/** `value` is a fraction: 0.0123 → "1.23%". */
export function formatPercent(value: Num, opts: NumberOptions = {}): string {
  if (!isNum(value)) return MISSING;
  const digits = opts.digits ?? 2;
  const text = numberFormat(activeFormat().locale, {
    style: 'percent',
    minimumFractionDigits: digits,
    maximumFractionDigits: digits,
  }).format(value);
  return signedText(text, value, opts.signed);
}

export function formatNumber(value: Num, opts: NumberOptions = {}): string {
  if (!isNum(value)) return MISSING;
  const text = numberFormat(activeFormat().locale, {
    notation: opts.compact ? 'compact' : 'standard',
    maximumFractionDigits: opts.digits ?? 4,
  }).format(value);
  return signedText(text, value, opts.signed);
}

/**
 * A calendar date. Date-only strings ("2026-09-26") are never shifted by the
 * time zone; timestamps are shown as the date in the preferred zone.
 * ISO style: "2026-09-26"; locale style: "Sep 26, 2026" / "26 Sept 2026".
 */
export function formatDate(value: string | null | undefined): string {
  if (!value) return MISSING;
  const { locale, timeZone, dateStyle } = activeFormat();
  const dateOnly = /^\d{4}-\d{2}-\d{2}$/.test(value);
  if (dateOnly && dateStyle === 'iso') return value;
  const d = new Date(dateOnly ? `${value}T00:00:00Z` : value);
  if (Number.isNaN(d.getTime())) return value;
  const zone = dateOnly ? 'UTC' : timeZone;
  if (dateStyle === 'iso') return isoDate(d, zone);
  return dateFormat(locale, { dateStyle: 'medium', timeZone: zone }).format(d);
}

/** Date and time in the preferred zone: "2026-09-26 14:05" or the locale's style. */
export function formatDateTime(value: string | null | undefined): string {
  if (!value) return MISSING;
  const d = new Date(value);
  if (Number.isNaN(d.getTime())) return value;
  const { locale, timeZone, dateStyle } = activeFormat();
  if (dateStyle === 'locale') {
    return dateFormat(locale, { dateStyle: 'medium', timeStyle: 'short', timeZone }).format(d);
  }
  const time = dateFormat('en-GB', {
    hour: '2-digit',
    minute: '2-digit',
    hourCycle: 'h23',
    timeZone,
  }).format(d);
  return `${isoDate(d, timeZone)} ${time}`;
}

function isoDate(d: Date, timeZone: string): string {
  // en-CA formats dates as YYYY-MM-DD.
  return dateFormat('en-CA', {
    year: 'numeric',
    month: '2-digit',
    day: '2-digit',
    timeZone,
  }).format(d);
}

/** "3m", "2h", "4d" ago; for recency columns. */
export function formatAgo(value: string | null | undefined, now: Date = new Date()): string {
  if (!value) return MISSING;
  const d = new Date(value);
  if (Number.isNaN(d.getTime())) return value;
  const s = Math.max(0, Math.round((now.getTime() - d.getTime()) / 1000));
  if (s < 60) return 'just now';
  if (s < 3600) return `${Math.floor(s / 60)}m ago`;
  if (s < 86400) return `${Math.floor(s / 3600)}h ago`;
  return `${Math.floor(s / 86400)}d ago`;
}

/** Duration between two timestamps: "850 ms", "12 s", "3 min". */
export function formatDuration(
  start: string | null | undefined,
  end: string | null | undefined,
): string {
  if (!start || !end) return MISSING;
  const ms = new Date(end).getTime() - new Date(start).getTime();
  if (!Number.isFinite(ms) || ms < 0) return MISSING;
  if (ms < 1000) return `${ms} ms`;
  if (ms < 60_000) return `${Math.round(ms / 1000)} s`;
  return `${Math.round(ms / 60_000)} min`;
}

/** CSS class for a signed figure: "gain" / "loss" / "". */
export function toneClass(value: Num): 'gain' | 'loss' | '' {
  if (!isNum(value) || value === 0) return '';
  return value > 0 ? 'gain' : 'loss';
}

function isNum(value: Num): value is number {
  return typeof value === 'number' && Number.isFinite(value);
}

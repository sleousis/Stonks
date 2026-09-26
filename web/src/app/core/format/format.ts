// Number and date formatting for the whole console. Use these (or the pipes
// in shared/format.pipes.ts) instead of ad-hoc toFixed() calls so every page
// shows figures the same way. Missing values render as an en dash.

/** The API does not report a portfolio currency yet; the console shows USD. */
export const DISPLAY_CURRENCY = 'USD';
export const MISSING = '–';

type Num = number | null | undefined;

const moneyFmt = new Intl.NumberFormat('en-US', {
  style: 'currency',
  currency: DISPLAY_CURRENCY,
  minimumFractionDigits: 2,
  maximumFractionDigits: 2,
});
const moneyCompactFmt = new Intl.NumberFormat('en-US', {
  style: 'currency',
  currency: DISPLAY_CURRENCY,
  notation: 'compact',
  maximumFractionDigits: 1,
});

export interface NumberOptions {
  /** Prefix positive values with "+" (changes, returns, P&L). */
  signed?: boolean;
  /** 1.2K / 3.4M (axes, tight tiles). */
  compact?: boolean;
  digits?: number;
}

export function formatMoney(value: Num, opts: NumberOptions = {}): string {
  if (!isNum(value)) return MISSING;
  const text = (opts.compact ? moneyCompactFmt : moneyFmt).format(value);
  return opts.signed && value > 0 ? `+${text}` : text;
}

/** `value` is a fraction: 0.0123 → "1.23%". */
export function formatPercent(value: Num, opts: NumberOptions = {}): string {
  if (!isNum(value)) return MISSING;
  const digits = opts.digits ?? 2;
  const text = new Intl.NumberFormat('en-US', {
    style: 'percent',
    minimumFractionDigits: digits,
    maximumFractionDigits: digits,
  }).format(value);
  return opts.signed && value > 0 ? `+${text}` : text;
}

export function formatNumber(value: Num, opts: NumberOptions = {}): string {
  if (!isNum(value)) return MISSING;
  const text = new Intl.NumberFormat('en-US', {
    notation: opts.compact ? 'compact' : 'standard',
    maximumFractionDigits: opts.digits ?? 4,
  }).format(value);
  return opts.signed && value > 0 ? `+${text}` : text;
}

/** "2026-09-26" (date-only strings are shown as-is, never shifted by time zone). */
export function formatDate(value: string | null | undefined): string {
  if (!value) return MISSING;
  if (/^\d{4}-\d{2}-\d{2}$/.test(value)) return value;
  const d = new Date(value);
  if (Number.isNaN(d.getTime())) return value;
  return d.toLocaleDateString('en-CA');
}

/** "2026-09-26 14:05" in the viewer's time zone. */
export function formatDateTime(value: string | null | undefined): string {
  if (!value) return MISSING;
  const d = new Date(value);
  if (Number.isNaN(d.getTime())) return value;
  return `${d.toLocaleDateString('en-CA')} ${d.toLocaleTimeString('en-GB', {
    hour: '2-digit',
    minute: '2-digit',
  })}`;
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

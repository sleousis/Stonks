import { formatNumber } from '../../../core/format/format';

/** 5400 seconds -> "90 min"; under a minute -> "under 1 min". */
export function minutesText(seconds: number | null | undefined): string {
  if (seconds == null || !Number.isFinite(seconds)) return 'n/a';
  if (seconds > 0 && seconds < 60) return 'under 1 min';
  return `${formatNumber(Math.round(seconds / 60), { digits: 0 })} min`;
}

/** "12 of 200". */
export function usedOf(used: number, max: number): string {
  return `${formatNumber(used, { digits: 0 })} of ${formatNumber(max, { digits: 0 })}`;
}

/** "the sp500 universe" or "3 tickers: AAA.US, BBB.US, CCC.US". */
export function universeText(universeId: string | null, universe: readonly string[]): string {
  if (universeId) return `The ${universeId} universe`;
  if (!universe.length) return 'No tickers';
  const n = universe.length;
  return `${n} ticker${n === 1 ? '' : 's'}: ${universe.join(', ')}`;
}

/** A value from the model's proposed arguments, as short text. */
export function argText(v: unknown): string {
  if (typeof v === 'number') return formatNumber(v);
  if (typeof v === 'string') return v;
  return JSON.stringify(v);
}

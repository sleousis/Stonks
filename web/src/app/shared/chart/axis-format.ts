import { formatMoney, formatNumber, formatPercent } from '../../core/format/format';
import type { ChartValueFormat } from './chart-engine';

const COMPACT_FROM = 100_000;
const NO_CENTS_FROM = 1_000;

/**
 * One label style for a whole price axis, picked once from the values it
 * shows, so an axis never mixes "$100K" with "$99,000.00".
 */
export function axisFormatter(
  format: ChartValueFormat | undefined,
  values: readonly number[],
): (v: number) => string {
  let top = 0;
  let lo = Infinity;
  let hi = -Infinity;
  for (const v of values) {
    if (!Number.isFinite(v)) continue;
    top = Math.max(top, Math.abs(v));
    lo = Math.min(lo, v);
    hi = Math.max(hi, v);
  }
  const compact = top >= COMPACT_FROM;
  switch (format) {
    case 'money':
      if (compact) return (v) => formatMoney(v, { compact: true });
      if (top >= NO_CENTS_FROM) return (v) => formatMoney(v, { digits: 0 });
      return (v) => formatMoney(v);
    case 'percent': {
      const digits = percentDigits(hi - lo);
      return (v) => formatPercent(v, { digits });
    }
    default:
      return (v) => formatNumber(v, { compact, digits: compact || top >= NO_CENTS_FROM ? 1 : 2 });
  }
}

/**
 * Decimals for a percent axis, from the range it spans, so the five or so
 * ticks differ ("-0.02%", "-0.04%", never "0.0%" three times). One decimal
 * from a 5-point range up, at most three.
 */
export function percentDigits(span: number): number {
  if (!Number.isFinite(span) || span <= 0) return 1;
  const step = (span * 100) / 5;
  return Math.min(3, Math.max(1, Math.ceil(-Math.log10(step))));
}

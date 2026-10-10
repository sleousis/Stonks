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
  for (const v of values) if (Number.isFinite(v)) top = Math.max(top, Math.abs(v));
  const compact = top >= COMPACT_FROM;
  switch (format) {
    case 'money':
      if (compact) return (v) => formatMoney(v, { compact: true });
      if (top >= NO_CENTS_FROM) return (v) => formatMoney(v, { digits: 0 });
      return (v) => formatMoney(v);
    case 'percent':
      return (v) => formatPercent(v, { digits: 1 });
    default:
      return (v) => formatNumber(v, { compact, digits: compact || top >= NO_CENTS_FROM ? 1 : 2 });
  }
}

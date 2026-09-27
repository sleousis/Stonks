import { formatMoney } from '../core/format/format';

/**
 * "Value in EUR: €9,120.00" when the portfolio's base currency differs from
 * the one its figures are shown in, or why there is no base total (a missing
 * FX rate, never guessed). Null when there is nothing to add. Insights and
 * the Today portfolio card use it.
 */
export function baseCurrencyLine(
  v: { currency: string; total_value_base?: number | null; fx_missing?: string[] },
  base: string | null | undefined,
): string | null {
  if (!base) return null;
  const missing = v.fx_missing ?? [];
  if (missing.length) {
    return `No exchange rate yet for ${missing.join(', ')}, so there is no total in ${base}.`;
  }
  if (base === v.currency || v.total_value_base == null) return null;
  return `Value in ${base}: ${formatMoney(v.total_value_base, { currency: base })}`;
}

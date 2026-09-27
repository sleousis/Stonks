import type { PortfolioRef } from '../../api/portfolios.service';
import { formatNumber } from '../../core/format/format';
import { humanize } from '../../shared/ui/param-form/param-spec';

/** Basis points to one decimal ("12.3 bps"), or "n/a" while not known yet. */
export function formatBps(value: number | null | undefined): string {
  if (value === null || value === undefined || !Number.isFinite(value)) return 'n/a';
  return `${formatNumber(value, { digits: 1, signed: true })} bps`;
}

/** Rounded to one decimal for sortable table cells; null stays null. */
export function bps1(value: number | null | undefined): number | null {
  if (value === null || value === undefined || !Number.isFinite(value)) return null;
  return Math.round(value * 10) / 10;
}

const TRIGGERS: Record<string, string> = {
  signal: 'Strategy signal',
  exit_no_pick: 'Exit with no new pick',
  risk_rule: 'Risk rule',
  manual: 'Placed by hand',
};

/** Why an order was placed, in plain words. */
export function triggerLabel(trigger: string | null | undefined): string {
  if (!trigger) return 'Not recorded';
  return TRIGGERS[trigger] ?? humanize(trigger);
}

/** A portfolio's name from the picker's list, or null when the list does not name it. */
export function portfolioName(
  id: string | null | undefined,
  options: readonly PortfolioRef[],
): string | null {
  if (!id) return null;
  return options.find((p) => p.id === id)?.name ?? null;
}

/** Paper or live for a portfolio when the list says so, else null (unknown). */
export function portfolioLive(
  id: string | null | undefined,
  options: readonly PortfolioRef[],
): boolean | null {
  const p = id ? options.find((o) => o.id === id) : options.find((o) => o.is_default);
  if (!p?.trading) return null;
  return p.trading === 'live';
}

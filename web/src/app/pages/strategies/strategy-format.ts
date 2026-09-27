import type { StrategyStatus } from '../../api/models';
import { STATUS_WORDS } from '../../shared/governance-labels';

export { shortClassName, strategyDisplayName, strategyKindName } from '../../shared/strategy-names';

/** Parameter values as compact text: numbers and strings as-is, the rest as JSON. */
export function formatParam(value: unknown): string {
  if (value === null || value === undefined) return '–';
  if (typeof value === 'string') return value;
  if (typeof value === 'number' || typeof value === 'boolean') return String(value);
  return JSON.stringify(value);
}

/** The list filters, in the trader's lifecycle words (UX-09). */
export const STATUS_FILTERS: readonly { value: StrategyStatus | null; label: string }[] = [
  { value: null, label: 'All' },
  { value: 'active', label: STATUS_WORDS.active },
  { value: 'shadow', label: STATUS_WORDS.shadow },
  { value: 'retired', label: STATUS_WORDS.retired },
];

export function asStatus(value: string | null | undefined): StrategyStatus | null {
  return value === 'active' || value === 'shadow' || value === 'retired' ? value : null;
}

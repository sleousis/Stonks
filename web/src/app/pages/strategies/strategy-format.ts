import type { StrategyStatus } from '../../api/models';

/** `stonks.strategies.momentum.MomentumStrategy` -> `MomentumStrategy`. */
export function shortClassName(classPath: string): string {
  return classPath.split('.').at(-1) || classPath;
}

/** Parameter values as compact text: numbers and strings as-is, the rest as JSON. */
export function formatParam(value: unknown): string {
  if (value === null || value === undefined) return '–';
  if (typeof value === 'string') return value;
  if (typeof value === 'number' || typeof value === 'boolean') return String(value);
  return JSON.stringify(value);
}

export const STATUS_FILTERS: readonly { value: StrategyStatus | null; label: string }[] = [
  { value: null, label: 'All' },
  { value: 'active', label: 'Active' },
  { value: 'shadow', label: 'Shadow' },
  { value: 'retired', label: 'Retired' },
];

export function asStatus(value: string | null | undefined): StrategyStatus | null {
  return value === 'active' || value === 'shadow' || value === 'retired' ? value : null;
}

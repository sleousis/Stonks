import type { StrategyStatus } from '../../api/models';

/** `stonks.strategies.momentum.MomentumStrategy` -> `MomentumStrategy`. */
export function shortClassName(classPath: string): string {
  return classPath.split('.').at(-1) || classPath;
}

/**
 * A readable name for the strategy's kind, from its class:
 * `stonks.strategies.momentum.MomentumStrategy` -> `Momentum strategy`,
 * `RSIMeanReversion` -> `RSI mean reversion`.
 */
export function strategyKindName(classPath: string): string {
  const words = shortClassName(classPath)
    .replace(/_/g, ' ')
    .replace(/([A-Z]+)([A-Z][a-z])/g, '$1 $2')
    .replace(/([a-z0-9])([A-Z])/g, '$1 $2')
    .trim()
    .split(/\s+/);
  return words
    .map((w, i) => {
      if (w.length > 1 && w === w.toUpperCase()) return w;
      return i === 0 ? w.charAt(0).toUpperCase() + w.slice(1) : w.toLowerCase();
    })
    .join(' ');
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

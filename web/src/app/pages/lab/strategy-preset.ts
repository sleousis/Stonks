import type { StrategyClassInfo, StrategyDetail } from '../../api/models';
import {
  type ParamValue,
  type ParamValues,
  defaultParamValues,
} from '../../shared/ui/param-form/param-spec';

/** A registered strategy the Lab re-runs (`/lab?strategy=<id>`). */
export interface StrategyPreset {
  strategyId: string;
  classPath: string;
  params: Record<string, unknown>;
}

export function presetFromStrategy(
  s: Pick<StrategyDetail, 'id' | 'class_path' | 'params'>,
): StrategyPreset {
  return { strategyId: s.id, classPath: s.class_path, params: s.params };
}

/**
 * The tickers a registered strategy trades, from its own settings
 * (`ticker`, `tickers` or `universe`), as the tickers field reads them.
 * Empty when it names none: it then runs on the production universe.
 */
export function presetTickers(params: Record<string, unknown>): string {
  const out: string[] = [];
  for (const key of ['ticker', 'tickers', 'universe']) {
    const v = params[key];
    if (typeof v === 'string') out.push(...v.split(/[\s,;]+/));
    else if (Array.isArray(v)) out.push(...v.filter((t): t is string => typeof t === 'string'));
  }
  return [...new Set(out.map((t) => t.trim().toUpperCase()).filter(Boolean))].join(', ');
}

/**
 * The class's defaults with the registered values laid over them. Only
 * declared parameters with a scalar value are taken, so a stale or odd
 * stored value can't reach the form.
 */
export function presetParamValues(
  cls: StrategyClassInfo,
  params: Record<string, unknown>,
): ParamValues {
  const values = defaultParamValues(cls.parameters);
  for (const p of cls.parameters) {
    const v = params[p.name];
    if (isParamValue(v)) values[p.name] = v;
  }
  return values;
}

function isParamValue(v: unknown): v is ParamValue {
  return (
    v === null ||
    typeof v === 'string' ||
    typeof v === 'boolean' ||
    (typeof v === 'number' && Number.isFinite(v))
  );
}

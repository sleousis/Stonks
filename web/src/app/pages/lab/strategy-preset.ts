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

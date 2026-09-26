import type { ParameterInfo } from '../../../api/models';

/**
 * Turns a strategy's declared parameter space (`ParameterInfo` from the
 * catalog, mirroring the backend `ParameterSpec`) into form fields, and
 * checks and coerces the values a form holds. Pure functions, so pages that
 * build their own UI (lab, studio) can reuse them without <app-param-form>.
 */

export type ParamValue = number | string | boolean | null;
export type ParamValues = Record<string, ParamValue>;

export type ParamField =
  | (ParamFieldBase<'int' | 'float'> & {
      min: number | null;
      max: number | null;
      step: number | 'any';
    })
  | ParamFieldBase<'bool'>
  | (ParamFieldBase<'choice'> & { choices: readonly (string | number | boolean)[] })
  | ParamFieldBase<'text'>;

interface ParamFieldBase<K extends string> {
  control: K;
  name: string;
  /** Human label from the snake_case name: `lookback_days` → "Lookback days". */
  label: string;
  description: string;
  tunable: boolean;
  defaultValue: ParamValue;
}

/** One form field per parameter, in declaration order. */
export function paramFields(params: readonly ParameterInfo[]): ParamField[] {
  return params.map(toField);
}

export function toField(p: ParameterInfo): ParamField {
  const base = {
    name: p.name,
    label: humanize(p.name),
    description: p.description ?? '',
    tunable: p.tunable,
  };
  const bounds = Array.isArray(p.bounds) ? p.bounds : null;
  switch (p.kind) {
    case 'int':
    case 'float': {
      const [lo, hi] = bounds && bounds.length === 2 ? bounds : [null, null];
      return {
        ...base,
        control: p.kind,
        min: typeof lo === 'number' ? lo : null,
        max: typeof hi === 'number' ? hi : null,
        step: p.kind === 'int' ? 1 : 'any',
        defaultValue: typeof p.default === 'number' ? p.default : null,
      };
    }
    case 'bool':
      return { ...base, control: 'bool', defaultValue: p.default === true };
    case 'categorical':
      // No bounds on a categorical means "any string" (e.g. a ticker).
      if (bounds && bounds.length) {
        const choices = bounds.filter(isScalar);
        return {
          ...base,
          control: 'choice',
          choices,
          defaultValue: isScalar(p.default) ? p.default : (choices[0] ?? null),
        };
      }
      return { ...base, control: 'text', defaultValue: textDefault(p.default) };
    default:
      return { ...base, control: 'text', defaultValue: textDefault(p.default) };
  }
}

/** Every parameter at its declared default. */
export function defaultParamValues(params: readonly ParameterInfo[]): ParamValues {
  return Object.fromEntries(paramFields(params).map((f) => [f.name, f.defaultValue]));
}

/** What is wrong with one value, or null when it is acceptable. */
export function paramError(field: ParamField, value: ParamValue | undefined): string | null {
  switch (field.control) {
    case 'int':
    case 'float': {
      if (value === null || value === undefined || value === '') return 'Enter a number.';
      if (typeof value !== 'number' || !Number.isFinite(value)) return 'Enter a number.';
      if (field.control === 'int' && !Number.isInteger(value)) return 'Enter a whole number.';
      if (field.min !== null && value < field.min) return `At least ${field.min}.`;
      if (field.max !== null && value > field.max) return `At most ${field.max}.`;
      return null;
    }
    case 'choice':
      return field.choices.some((c) => c === value) ? null : 'Pick one of the choices.';
    case 'text':
      return typeof value === 'string' && value.trim() ? null : 'Enter a value.';
    case 'bool':
      return null;
  }
}

/** Field name → error, only for fields that have one. */
export function paramErrors(
  params: readonly ParameterInfo[],
  values: ParamValues,
): Record<string, string> {
  const out: Record<string, string> = {};
  for (const f of paramFields(params)) {
    const err = paramError(f, values[f.name]);
    if (err) out[f.name] = err;
  }
  return out;
}

/**
 * Values ready for a request body: known names only, strings trimmed.
 * Values left at their default are included too, so the request is explicit.
 */
export function paramPayload(
  params: readonly ParameterInfo[],
  values: ParamValues,
): Record<string, ParamValue> {
  const out: Record<string, ParamValue> = {};
  for (const f of paramFields(params)) {
    const v = values[f.name] ?? f.defaultValue;
    out[f.name] = typeof v === 'string' ? v.trim() : v;
  }
  return out;
}

/** Short range text for hints: "1 – 250", "≥ 0", "one of fast, slow". */
export function rangeText(field: ParamField): string | null {
  if (field.control === 'int' || field.control === 'float') {
    if (field.min !== null && field.max !== null) return `${field.min} – ${field.max}`;
    if (field.min !== null) return `≥ ${field.min}`;
    if (field.max !== null) return `≤ ${field.max}`;
  }
  return null;
}

/** `lookback_days` → "Lookback days". */
export function humanize(name: string): string {
  const words = name.replace(/[_-]+/g, ' ').trim();
  return words ? words.charAt(0).toUpperCase() + words.slice(1) : name;
}

function isScalar(v: unknown): v is string | number | boolean {
  return typeof v === 'string' || typeof v === 'number' || typeof v === 'boolean';
}

function textDefault(v: unknown): string {
  return v === null || v === undefined ? '' : String(v);
}

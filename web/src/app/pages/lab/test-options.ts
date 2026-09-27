import type { SurvivalTestInfo } from '../../api/models';

/**
 * The "advanced test options" editor, driven by `GET /api/lab/survival-tests`:
 * each test's `options_schema` (JSON Schema of the backend's own options
 * model) becomes form fields, validated in the browser so mistakes show next
 * to the field, and turned into the request's `test_options`.
 *
 * Every field starts blank, which means "the test's default"; only fields
 * the trader fills are sent. No option names live here: labels, defaults
 * and bounds all come from the schema.
 */

export type OptionKind =
  'number' | 'int' | 'choice' | 'text' | 'list-number' | 'list-int' | 'list-text';

export interface OptionChoice {
  /** JSON of the value to send (`"val"`, `true`), so any type fits a `<select>`. */
  value: string;
  label: string;
}

export interface OptionField {
  key: string;
  label: string;
  kind: OptionKind;
  /** Shown as the placeholder or the blank choice: what blank means. */
  defaultText: string;
  hint?: string;
  min?: number;
  max?: number;
  /** Bounds are exclusive (strictly greater / less). */
  exclusiveMin?: boolean;
  exclusiveMax?: boolean;
  choices?: readonly OptionChoice[];
}

/** Fields per test id; tests with nothing to set are left out. */
export type OptionCatalog = Readonly<Record<string, readonly OptionField[]>>;

/** Raw text per test, per field, as typed. */
export type TestOptionValues = Readonly<Record<string, Readonly<Record<string, string>>>>;

type Schema = Record<string, unknown>;

/** Short forms a trader knows, shown in capitals. */
const ACRONYMS = new Set([
  'ir',
  'psr',
  'dsr',
  'psi',
  'pbo',
  'cagr',
  'oos',
  'pnl',
  'dd',
  'wfe',
  'ic',
  'mc',
  'var',
  'es',
]);

function word(w: string): string {
  const lower = w.toLowerCase();
  return ACRONYMS.has(lower) ? lower.toUpperCase() : lower;
}

/** "Min Psr" → "Min PSR", "N Permutations" → "Number of permutations". */
export function optionLabel(key: string, title: string | undefined): string {
  const words = (title?.trim() || key)
    .split(/[\s_]+/)
    .filter(Boolean)
    .map(word);
  if (words[0] === 'n' && words.length > 1) words.splice(0, 1, 'number', 'of');
  const text = words.join(' ');
  return text.charAt(0).toUpperCase() + text.slice(1);
}

function valueLabel(v: unknown): string {
  if (v === true) return 'Yes';
  if (v === false) return 'No';
  if (v === null) return 'Not set';
  if (typeof v === 'string') return optionLabel(v, undefined);
  return String(v);
}

function defaultText(schema: Schema, kind: OptionKind): string {
  if (!('default' in schema)) return 'Default';
  const d = schema['default'];
  if (Array.isArray(d)) return d.length ? d.join(', ') : 'None';
  if (d === null) return 'Not set';
  if (kind === 'choice') return valueLabel(d);
  return String(d);
}

function num(v: unknown): number | undefined {
  return typeof v === 'number' ? v : undefined;
}

/** "At least 0.8 and at most 0.99" (no trailing stop), or "" without bounds. */
function describeRange(f: OptionField): string {
  const lo = f.min === undefined ? null : `${f.exclusiveMin ? 'above' : 'at least'} ${f.min}`;
  const hi = f.max === undefined ? null : `${f.exclusiveMax ? 'below' : 'at most'} ${f.max}`;
  return [lo, hi].filter(Boolean).join(' and ');
}

/** The non-null variants of a property (`anyOf: [X, null]` → X). */
function variants(p: Schema): Schema[] {
  const any = (p['anyOf'] ?? p['oneOf']) as Schema[] | undefined;
  const list = Array.isArray(any) ? any : [p];
  return list.filter((v) => v['type'] !== 'null');
}

function choicesOf(vs: Schema[]): OptionChoice[] | null {
  const values: unknown[] = [];
  for (const v of vs) {
    if (Array.isArray(v['enum'])) values.push(...(v['enum'] as unknown[]));
    else if ('const' in v) values.push(v['const']);
    else if (v['type'] === 'boolean') values.push(true, false);
    else return null;
  }
  return values.length
    ? values.map((x) => ({ value: JSON.stringify(x), label: valueLabel(x) }))
    : null;
}

function toField(key: string, p: Schema): OptionField | null {
  const vs = variants(p);
  if (!vs.length) return null;
  const base = { key, label: optionLabel(key, p['title'] as string | undefined) };
  const choices = choicesOf(vs);
  let field: OptionField;
  if (choices) {
    field = { ...base, kind: 'choice', choices, defaultText: '' };
  } else if (vs.length === 1) {
    const v = vs[0];
    const bounds = {
      min: num(v['minimum']) ?? num(v['exclusiveMinimum']),
      max: num(v['maximum']) ?? num(v['exclusiveMaximum']),
      exclusiveMin: v['exclusiveMinimum'] !== undefined || undefined,
      exclusiveMax: v['exclusiveMaximum'] !== undefined || undefined,
    };
    switch (v['type']) {
      case 'number':
      case 'integer':
        field = {
          ...base,
          kind: v['type'] === 'integer' ? 'int' : 'number',
          defaultText: '',
          ...bounds,
        };
        break;
      case 'string':
        field = { ...base, kind: 'text', defaultText: '' };
        break;
      case 'array': {
        const items = (v['items'] ?? {}) as Schema;
        const kind =
          items['type'] === 'integer'
            ? 'list-int'
            : items['type'] === 'number'
              ? 'list-number'
              : items['type'] === 'string'
                ? 'list-text'
                : null;
        if (!kind) return null;
        field = { ...base, kind, defaultText: '' };
        break;
      }
      default:
        return null;
    }
  } else {
    return null;
  }
  for (const k of ['min', 'max', 'exclusiveMin', 'exclusiveMax'] as const) {
    if (field[k] === undefined) delete field[k];
  }
  field.defaultText = defaultText(p, field.kind);
  const range = describeRange(field);
  const list = field.kind.startsWith('list-') ? 'Separate with commas.' : '';
  const description = typeof p['description'] === 'string' ? p['description'] : '';
  const hint = [description, range && `${range.charAt(0).toUpperCase()}${range.slice(1)}.`, list]
    .filter(Boolean)
    .join(' ');
  if (hint) field.hint = hint;
  return field;
}

/** Form fields for one test's `options_schema`, in schema order. */
export function optionFields(schema: Schema): OptionField[] {
  const props = (schema['properties'] ?? {}) as Record<string, Schema>;
  return Object.entries(props)
    .map(([key, p]) => toField(key, p))
    .filter((f): f is OptionField => f !== null);
}

/** Fields for every test in the API's catalog; `skip` tests have their own form section. */
export function optionCatalog(
  tests: readonly SurvivalTestInfo[],
  skip: readonly string[] = [],
): OptionCatalog {
  const out: Record<string, OptionField[]> = {};
  for (const t of tests) {
    if (skip.includes(t.id)) continue;
    const fields = optionFields(t.options_schema);
    if (fields.length) out[t.id] = fields;
  }
  return out;
}

function inRange(f: OptionField, n: number): boolean {
  const low = f.min !== undefined && (f.exclusiveMin ? n <= f.min : n < f.min);
  const high = f.max !== undefined && (f.exclusiveMax ? n >= f.max : n > f.max);
  return !low && !high;
}

/** The value to send, or an error message. Blank is `{}` (use the default). */
export function parseOption(
  f: OptionField,
  raw: string | undefined,
): { value?: unknown; error?: string } {
  const text = (raw ?? '').trim();
  if (!text) return {};
  switch (f.kind) {
    case 'choice': {
      const hit = f.choices?.find((c) => c.value === text);
      return hit ? { value: JSON.parse(hit.value) } : { error: 'Pick one of the listed options.' };
    }
    case 'text':
      return { value: text };
    case 'list-text':
      return { value: splitList(text) };
    case 'list-int':
    case 'list-number': {
      const nums = splitList(text).map(Number);
      if (nums.some((n) => !Number.isFinite(n)))
        return { error: 'Enter numbers separated by commas.' };
      if (f.kind === 'list-int' && nums.some((n) => !Number.isInteger(n)))
        return { error: 'Enter whole numbers separated by commas.' };
      return { value: nums };
    }
    case 'int':
    case 'number': {
      const n = Number(text);
      if (!Number.isFinite(n)) return { error: 'Enter a number.' };
      if (f.kind === 'int' && !Number.isInteger(n)) return { error: 'Enter a whole number.' };
      if (!inRange(f, n)) return { error: `Must be ${describeRange(f)}.` };
      return { value: n };
    }
  }
}

function splitList(text: string): string[] {
  return text
    .split(/[\s,;]+/)
    .map((t) => t.trim())
    .filter(Boolean);
}

/** Field errors keyed `<test>.<field>`, for the tests in `suite` only. */
export function testOptionErrors(
  suite: readonly string[],
  values: TestOptionValues,
  catalog: OptionCatalog,
): Record<string, string> {
  const errors: Record<string, string> = {};
  for (const test of suite) {
    for (const f of catalog[test] ?? []) {
      const { error } = parseOption(f, values[test]?.[f.key]);
      if (error) errors[`${test}.${f.key}`] = error;
    }
  }
  return errors;
}

/**
 * `test_options` for the request: only tests in the suite, only filled
 * fields. `null` when nothing is set (the request then omits it).
 */
export function buildTestOptions(
  suite: readonly string[],
  values: TestOptionValues,
  catalog: OptionCatalog,
): Record<string, Record<string, unknown>> | null {
  const out: Record<string, Record<string, unknown>> = {};
  for (const test of suite) {
    const opts: Record<string, unknown> = {};
    for (const f of catalog[test] ?? []) {
      const { value, error } = parseOption(f, values[test]?.[f.key]);
      if (value !== undefined && !error) opts[f.key] = value;
    }
    if (Object.keys(opts).length) out[test] = opts;
  }
  return Object.keys(out).length ? out : null;
}

/** How many fields of a test are filled in. */
export function filledCount(values: TestOptionValues, test: string): number {
  return Object.values(values[test] ?? {}).filter((v) => v.trim() !== '').length;
}

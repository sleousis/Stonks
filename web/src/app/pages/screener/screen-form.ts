import type { MetricView, ScreenSpec } from '../../api/models';
import { formatNumber, formatPercent } from '../../core/format/format';

export type AssetClass = 'equity' | 'crypto' | 'commodity' | 'bond';

export const ASSET_CLASSES: readonly { value: AssetClass; label: string }[] = [
  { value: 'equity', label: 'Stocks' },
  { value: 'crypto', label: 'Crypto' },
  { value: 'commodity', label: 'Commodities' },
  { value: 'bond', label: 'Bonds' },
];

/** One metric bound, as typed. Percent metrics are typed in percent (8 means 8%). */
export interface FilterRow {
  key: number;
  metric: string;
  min: string;
  max: string;
}

/** The screen as the form holds it: text as typed, turned into a spec on run. */
export interface ScreenForm {
  universeId: string;
  assetClasses: AssetClass[];
  sectors: string;
  excludeSectors: string;
  exchanges: string;
  minPrice: string;
  minAdv: string;
  filters: FilterRow[];
  sortBy: string;
  descending: boolean;
  limit: string;
  columns: string[];
}

/** The API's limits (`screener/spec.py`). */
export const MAX_FILTERS = 20;
export const MAX_LIMIT = 5000;

let nextKey = 1;
export function filterRow(metric = '', min = '', max = ''): FilterRow {
  return { key: nextKey++, metric, min, max };
}

export function emptyForm(): ScreenForm {
  return {
    universeId: '',
    assetClasses: [],
    sectors: '',
    excludeSectors: '',
    exchanges: '',
    minPrice: '',
    minAdv: '',
    filters: [],
    sortBy: '',
    descending: true,
    limit: '50',
    columns: [],
  };
}

function list(text: string, upper = false): string[] {
  const out: string[] = [];
  for (const raw of text.split(/[,;\n]+/)) {
    const v = upper ? raw.trim().toUpperCase() : raw.trim();
    if (v && !out.includes(v)) out.push(v);
  }
  return out;
}

function unitOf(metrics: readonly MetricView[], id: string): MetricView['unit'] | undefined {
  return metrics.find((m) => m.id === id)?.unit;
}

/** A typed bound as the API takes it: percent metrics as a fraction. */
function bound(text: string, unit: MetricView['unit'] | undefined): number | null | 'bad' {
  const t = text.trim();
  if (!t) return null;
  const n = Number(t);
  if (!Number.isFinite(n)) return 'bad';
  return unit === 'percent' ? n / 100 : n;
}

/** A fraction as the form shows it, in percent for percent metrics, without float noise. */
function typed(value: number | null | undefined, unit: MetricView['unit'] | undefined): string {
  if (value === null || value === undefined) return '';
  const v = unit === 'percent' ? value * 100 : value;
  return String(Number(v.toPrecision(12)));
}

function positive(text: string): number | null | 'bad' {
  const t = text.trim();
  if (!t) return null;
  const n = Number(t);
  return Number.isFinite(n) && n >= 0 ? n : 'bad';
}

export interface SpecResult {
  spec: ScreenSpec;
  /** Plain words, one per problem; the spec is not sent while any remain. */
  errors: string[];
}

/** The form as a spec, with what is wrong in words. */
export function toSpec(form: ScreenForm, metrics: readonly MetricView[]): SpecResult {
  const errors: string[] = [];
  const label = (id: string) => metrics.find((m) => m.id === id)?.label ?? id;
  const spec: ScreenSpec = {};
  if (form.universeId) spec.universe_id = form.universeId;
  if (form.assetClasses.length) spec.asset_classes = [...form.assetClasses];
  const sectors = list(form.sectors);
  if (sectors.length) spec.sectors = sectors;
  const exclude = list(form.excludeSectors);
  if (exclude.length) spec.exclude_sectors = exclude;
  const exchanges = list(form.exchanges, true);
  if (exchanges.length) spec.exchanges = exchanges;
  const minPrice = positive(form.minPrice);
  if (minPrice === 'bad') errors.push('Lowest price must be a number, zero or more.');
  else if (minPrice !== null) spec.min_price = minPrice;
  const minAdv = positive(form.minAdv);
  if (minAdv === 'bad') errors.push('Lowest daily dollar volume must be a number, zero or more.');
  else if (minAdv !== null) spec.min_adv = minAdv;

  const filters: NonNullable<ScreenSpec['filters']> = [];
  for (const row of form.filters) {
    if (!row.metric) {
      errors.push('Pick a metric for each filter, or remove the empty one.');
      continue;
    }
    const unit = unitOf(metrics, row.metric);
    const min = bound(row.min, unit);
    const max = bound(row.max, unit);
    if (min === 'bad' || max === 'bad') {
      errors.push(`${label(row.metric)}: the bounds must be numbers.`);
      continue;
    }
    if (min === null && max === null) {
      errors.push(`${label(row.metric)}: give a minimum, a maximum or both.`);
      continue;
    }
    if (min !== null && max !== null && min > max) {
      errors.push(`${label(row.metric)}: the minimum is above the maximum.`);
      continue;
    }
    filters.push({ metric: row.metric, min, max });
  }
  if (filters.length > MAX_FILTERS) errors.push(`Use at most ${MAX_FILTERS} filters.`);
  if (filters.length) spec.filters = filters;

  if (form.sortBy) {
    spec.sort_by = form.sortBy;
    spec.descending = form.descending;
  }
  const limit = form.limit.trim();
  if (limit) {
    const n = Number(limit);
    if (!Number.isInteger(n) || n < 1 || n > MAX_LIMIT) {
      errors.push(`Show between 1 and ${MAX_LIMIT} rows, or leave it empty for all.`);
    } else {
      spec.limit = n;
    }
  }
  if (form.columns.length > MAX_FILTERS) errors.push(`Show at most ${MAX_FILTERS} extra columns.`);
  if (form.columns.length) spec.columns = [...form.columns];
  return { spec, errors: [...new Set(errors)] };
}

/** A saved spec back in the form. */
export function fromSpec(spec: ScreenSpec, metrics: readonly MetricView[]): ScreenForm {
  return {
    universeId: spec.universe_id ?? '',
    assetClasses: [...(spec.asset_classes ?? [])],
    sectors: (spec.sectors ?? []).join(', '),
    excludeSectors: (spec.exclude_sectors ?? []).join(', '),
    exchanges: (spec.exchanges ?? []).join(', '),
    minPrice: typed(spec.min_price, undefined),
    minAdv: typed(spec.min_adv, undefined),
    filters: (spec.filters ?? []).map((f) => {
      const unit = unitOf(metrics, f.metric);
      return filterRow(f.metric, typed(f.min, unit), typed(f.max, unit));
    }),
    sortBy: spec.sort_by ?? '',
    descending: spec.descending ?? true,
    limit: spec.limit ? String(spec.limit) : '',
    columns: [...(spec.columns ?? [])],
  };
}

/** A metric value for a cell: percents as percents, money and ratios as numbers. */
export function formatMetric(value: number | null | undefined, unit: MetricView['unit']): string {
  if (unit === 'percent') return formatPercent(value, { digits: 1 });
  if (unit === 'money') return formatNumber(value, { compact: true });
  return formatNumber(value, { digits: 2 });
}

/** A universe id from a name: `Cheap dividend payers` to `cheap-dividend-payers`. */
export function universeSlug(name: string): string {
  const slug = name
    .toLowerCase()
    .normalize('NFKD')
    .replace(/[^a-z0-9]+/g, '-')
    .replace(/^-+|-+$/g, '')
    .slice(0, 60)
    .replace(/-+$/g, '');
  return slug || 'my-screen';
}

/** The API's universe id rule. */
export const UNIVERSE_ID = /^[a-z0-9][a-z0-9_.-]{0,63}$/;

import type { UniverseCreate, UniverseView } from '../../api/models';

export type UniverseKind = UniverseView['kind'];
/** Where the definition comes from: the kind's fields, a CSV (lists), or JSON (advanced). */
export type SpecSource = 'fields' | 'csv' | 'json';
export type Rebalance = 'weekly' | 'monthly' | 'quarterly';

export const KIND_LABEL: Record<UniverseKind, string> = {
  list: 'List',
  exchange: 'Exchange',
  rule: 'Rule',
  index: 'Index',
};

export const KIND_HINT: Record<UniverseKind, string> = {
  list: 'Fixed tickers, or dated spans. Without spans a list has survivorship bias.',
  exchange: 'Every symbol a source lists on an exchange, delisted ones too.',
  rule: 'Filters checked against our price data at each rebalance date.',
  index: 'Index members rebuilt from an imported change history.',
};

export const REBALANCE_LABEL: Record<Rebalance, string> = {
  weekly: 'Weekly',
  monthly: 'Monthly',
  quarterly: 'Quarterly',
};

export const ASSET_CLASS_LABEL: Record<string, string> = {
  equity: 'Stocks',
  crypto: 'Crypto',
  commodity: 'Commodities',
  bond: 'Bonds',
};

/**
 * The fields a trader fills per kind (docs/universes.md). Kept as strings,
 * as the inputs hold them; `specFromFields` turns them into the spec.
 */
export interface KindFields {
  /** list: tickers, commas, spaces or new lines. */
  tickers: string;
  /** list, exchange, index: members from this date. */
  startDate: string;
  /** exchange */
  exchange: string;
  dataSource: string;
  includeDelisted: boolean;
  /** rule */
  start: string;
  end: string;
  rebalance: Rebalance;
  minAdv: string;
  minPrice: string;
  assetClasses: string[];
  /** index */
  indexId: string;
  indexSource: string;
}

export const DEFAULT_FIELDS: KindFields = {
  tickers: 'AAPL.US, MSFT.US',
  startDate: '2020-01-01',
  exchange: 'US',
  dataSource: 'eodhd',
  includeDelisted: true,
  start: '2020-01-01',
  end: '',
  rebalance: 'monthly',
  minAdv: '1000000',
  minPrice: '5',
  assetClasses: ['equity'],
  indexId: 'sp500',
  indexSource: 'wikipedia_sp500',
};

/** "aapl.us, msft.us" -> ["AAPL.US", "MSFT.US"], de-duplicated. */
export function tickerList(text: string): string[] {
  const seen = new Set<string>();
  for (const part of text.split(/[\s,;]+/)) {
    const t = part.trim().toUpperCase();
    if (t) seen.add(t);
  }
  return [...seen];
}

function numberOrNull(text: string): number | null {
  const t = text.trim();
  if (!t) return null;
  const n = Number(t);
  return Number.isFinite(n) ? n : null;
}

/** The spec the API takes, from the kind's fields. Blank optional fields are left out. */
export function specFromFields(kind: UniverseKind, f: KindFields): Record<string, unknown> {
  const withStart = (spec: Record<string, unknown>) =>
    f.startDate ? { ...spec, start_date: f.startDate } : spec;
  switch (kind) {
    case 'list':
      return withStart({ tickers: tickerList(f.tickers) });
    case 'exchange':
      return withStart({
        exchange: f.exchange.trim().toUpperCase(),
        source: f.dataSource,
        include_delisted: f.includeDelisted,
      });
    case 'index':
      return withStart({ index_id: f.indexId.trim(), source: f.indexSource.trim() });
    case 'rule': {
      const spec: Record<string, unknown> = { rebalance: f.rebalance };
      if (f.start) spec['start'] = f.start;
      spec['end'] = f.end || null;
      const adv = numberOrNull(f.minAdv);
      const price = numberOrNull(f.minPrice);
      if (adv !== null) spec['min_adv'] = adv;
      if (price !== null) spec['min_price'] = price;
      if (f.assetClasses.length) spec['asset_classes'] = [...f.assetClasses];
      return spec;
    }
  }
}

/** A starting spec per kind, for the JSON box (docs/universes.md). */
export function specTemplate(kind: UniverseKind, f: KindFields = DEFAULT_FIELDS): string {
  return JSON.stringify(specFromFields(kind, f), null, 2);
}

export interface UniverseForm {
  id: string;
  name: string;
  description: string;
  kind: UniverseKind;
  source: SpecSource;
  fields: KindFields;
  specText: string;
  csv: string;
}

export type UniverseFormErrors = Partial<
  Record<
    'id' | 'spec' | 'csv' | 'tickers' | 'exchange' | 'indexId' | 'minAdv' | 'minPrice' | 'window',
    string
  >
>;

const ID_PATTERN = /^[a-z0-9][a-z0-9_.-]*$/i;

/** The spec as an object, or the reason it is not one. */
export function parseSpec(text: string): { spec: Record<string, unknown> } | { error: string } {
  if (!text.trim()) return { spec: {} };
  try {
    const value: unknown = JSON.parse(text);
    if (value === null || typeof value !== 'object' || Array.isArray(value)) {
      return { error: 'The definition must be a JSON object, like {"tickers": ["AAPL.US"]}.' };
    }
    return { spec: value as Record<string, unknown> };
  } catch {
    return { error: 'The definition is not valid JSON.' };
  }
}

function fieldErrors(kind: UniverseKind, f: KindFields): UniverseFormErrors {
  const errors: UniverseFormErrors = {};
  if (kind === 'list' && tickerList(f.tickers).length === 0) {
    errors.tickers = 'Enter at least one ticker.';
  }
  if (kind === 'exchange' && !f.exchange.trim()) errors.exchange = 'Enter an exchange, like US.';
  if (kind === 'index' && !f.indexId.trim()) errors.indexId = 'Enter the index id, like sp500.';
  if (kind === 'rule') {
    const bad = (t: string) => !!t.trim() && (numberOrNull(t) === null || Number(t) < 0);
    if (bad(f.minAdv)) errors.minAdv = 'Enter a number of 0 or more, or leave it blank.';
    if (bad(f.minPrice)) errors.minPrice = 'Enter a number of 0 or more, or leave it blank.';
    if (f.start && f.end && f.start > f.end)
      errors.window = 'The start must be on or before the end.';
  }
  return errors;
}

export function universeFormErrors(f: UniverseForm): UniverseFormErrors {
  const errors: UniverseFormErrors = {};
  const id = f.id.trim();
  if (!id) errors.id = 'Enter an id.';
  else if (!ID_PATTERN.test(id)) errors.id = 'Use letters, digits, dots, dashes or underscores.';
  if (f.source === 'csv') {
    if (!f.csv.trim()) errors.csv = 'Choose a CSV file.';
  } else if (f.source === 'json') {
    const parsed = parseSpec(f.specText);
    if ('error' in parsed) errors.spec = parsed.error;
  } else {
    Object.assign(errors, fieldErrors(f.kind, f.fields));
  }
  return errors;
}

/** The request body. CSV only applies to list universes. */
export function universeCreateBody(f: UniverseForm): UniverseCreate {
  let spec: Record<string, unknown> = {};
  if (f.source === 'fields') spec = specFromFields(f.kind, f.fields);
  if (f.source === 'json') {
    const parsed = parseSpec(f.specText);
    spec = 'spec' in parsed ? parsed.spec : {};
  }
  return {
    id: f.id.trim(),
    kind: f.kind,
    name: f.name.trim() || null,
    description: f.description.trim() || null,
    spec,
    csv: f.source === 'csv' ? f.csv : null,
  };
}

/** Read an uploaded file as text (CSV or JSON). */
export function readFileText(file: File): Promise<string> {
  if (typeof file.text === 'function') return file.text();
  return new Promise((resolve, reject) => {
    const reader = new FileReader();
    reader.onload = () => resolve(String(reader.result ?? ''));
    reader.onerror = () => reject(reader.error);
    reader.readAsText(file);
  });
}

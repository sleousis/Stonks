import type {
  MetricView,
  ScreenSpec,
  UniverseCreate,
  UniverseUpdate,
  UniverseView,
} from '../../api/models';
import { type ScreenForm, emptyForm, fromSpec, toSpec } from '../screener/screen-form';

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
  rule: 'A screen checked against our data at each rebalance date.',
  index: 'Index members rebuilt from an imported change history.',
};

export const REBALANCE_LABEL: Record<Rebalance, string> = {
  weekly: 'Weekly',
  monthly: 'Monthly',
  quarterly: 'Quarterly',
};

/** The first day of an open-ended membership (the API's `EARLIEST`). */
export const EARLIEST = '1900-01-01';

/**
 * The fields a trader fills per kind (docs/universes.md). Kept as strings,
 * as the inputs hold them. `specFromFields` turns them into the spec.
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
  /** rule: the window and how often the screen runs */
  start: string;
  end: string;
  rebalance: Rebalance;
  /** rule: the screen itself, as the screener's form holds it. */
  screen: ScreenForm;
  /** index */
  indexId: string;
  indexSource: string;
}

/** A rule's screen to start from: liquid stocks, no top N. */
export function defaultScreen(): ScreenForm {
  return {
    ...emptyForm(),
    assetClasses: ['equity'],
    minAdv: '1000000',
    minPrice: '5',
    limit: '',
  };
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
  screen: defaultScreen(),
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

/** The rule's screen as spec keys. `columns` only matter to the screener. */
function ruleScreen(
  screen: ScreenForm,
  metrics: readonly MetricView[],
): { spec: ScreenSpec; errors: string[] } {
  const { spec, errors } = toSpec({ ...screen, columns: [] }, metrics);
  return { spec, errors };
}

/** The spec the API takes, from the kind's fields. Blank optional fields are left out. */
export function specFromFields(
  kind: UniverseKind,
  f: KindFields,
  metrics: readonly MetricView[] = [],
): Record<string, unknown> {
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
      return { ...spec, ...ruleScreen(f.screen, metrics).spec };
    }
  }
}

/** The spec keys each kind's fields can show. Anything else opens the JSON editor. */
const FIELD_KEYS: Record<UniverseKind, readonly string[]> = {
  list: ['tickers', 'start_date'],
  exchange: ['exchange', 'source', 'include_delisted', 'start_date'],
  index: ['index_id', 'source', 'start_date'],
  rule: [
    'rebalance',
    'start',
    'end',
    'universe_id',
    'asset_classes',
    'sectors',
    'exclude_sectors',
    'exchanges',
    'min_price',
    'min_adv',
    'filters',
    'sort_by',
    'descending',
    'limit',
  ],
};

function text(value: unknown): string {
  return value === null || value === undefined ? '' : String(value);
}

/** A stored day as the date input takes it: the open-ended start shows blank. */
function day(value: unknown): string {
  const t = text(value);
  return t === EARLIEST ? '' : t;
}

/**
 * A stored definition back in the form: the kind's fields when they can
 * hold every setting, else the JSON editor so nothing is lost on save.
 */
export function formFromUniverse(
  u: Pick<UniverseView, 'id' | 'kind' | 'name' | 'description' | 'spec'>,
  metrics: readonly MetricView[] = [],
): UniverseForm {
  const spec = (u.spec ?? {}) as Record<string, unknown>;
  const known = FIELD_KEYS[u.kind];
  const extra = Object.keys(spec).some((k) => !known.includes(k));
  const f: KindFields = { ...DEFAULT_FIELDS, screen: defaultScreen() };
  switch (u.kind) {
    case 'list':
      f.tickers = ((spec['tickers'] as string[] | undefined) ?? []).join(', ');
      f.startDate = day(spec['start_date']);
      break;
    case 'exchange':
      f.exchange = text(spec['exchange']);
      f.dataSource = text(spec['source']) || DEFAULT_FIELDS.dataSource;
      f.includeDelisted = spec['include_delisted'] !== false;
      f.startDate = day(spec['start_date']);
      break;
    case 'index':
      f.indexId = text(spec['index_id']);
      f.indexSource = text(spec['source']);
      f.startDate = day(spec['start_date']);
      break;
    case 'rule':
      f.start = text(spec['start']);
      f.end = text(spec['end']);
      f.rebalance = (spec['rebalance'] as Rebalance | undefined) ?? 'monthly';
      f.screen = { ...fromSpec(spec as ScreenSpec, metrics), columns: [] };
      break;
  }
  return {
    id: u.id,
    name: u.name ?? '',
    description: u.description ?? '',
    kind: u.kind,
    source: extra ? 'json' : 'fields',
    fields: f,
    specText: JSON.stringify(spec, null, 2),
    csv: '',
  };
}

/** A starting spec per kind, for the JSON box (docs/universes.md). */
export function specTemplate(
  kind: UniverseKind,
  f: KindFields = DEFAULT_FIELDS,
  metrics: readonly MetricView[] = [],
): string {
  return JSON.stringify(specFromFields(kind, f, metrics), null, 2);
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
  Record<'id' | 'spec' | 'csv' | 'tickers' | 'exchange' | 'indexId' | 'screen' | 'window', string>
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

function fieldErrors(
  kind: UniverseKind,
  f: KindFields,
  metrics: readonly MetricView[],
): UniverseFormErrors {
  const errors: UniverseFormErrors = {};
  if (kind === 'list' && tickerList(f.tickers).length === 0) {
    errors.tickers = 'Enter at least one ticker.';
  }
  if (kind === 'exchange' && !f.exchange.trim()) errors.exchange = 'Enter an exchange, like US.';
  if (kind === 'index' && !f.indexId.trim()) errors.indexId = 'Enter the index id, like sp500.';
  if (kind === 'rule') {
    if (!f.start) errors.window = 'Pick the first day the rule runs.';
    else if (f.end && f.start > f.end) errors.window = 'The start must be on or before the end.';
    const screen = ruleScreen(f.screen, metrics).errors;
    if (screen.length) errors.screen = screen.join(' ');
  }
  return errors;
}

export function universeFormErrors(
  f: UniverseForm,
  metrics: readonly MetricView[] = [],
): UniverseFormErrors {
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
    Object.assign(errors, fieldErrors(f.kind, f.fields, metrics));
  }
  return errors;
}

/** The new definition, for an edit. CSV only applies to list universes. */
export function universeUpdateBody(
  f: UniverseForm,
  metrics: readonly MetricView[] = [],
): UniverseUpdate {
  let spec: Record<string, unknown> = {};
  if (f.source === 'fields') spec = specFromFields(f.kind, f.fields, metrics);
  if (f.source === 'json') {
    const parsed = parseSpec(f.specText);
    spec = 'spec' in parsed ? parsed.spec : {};
  }
  return {
    kind: f.kind,
    name: f.name.trim() || null,
    description: f.description.trim() || null,
    spec,
    csv: f.source === 'csv' ? f.csv : null,
  };
}

/** The request body for a new universe. */
export function universeCreateBody(
  f: UniverseForm,
  metrics: readonly MetricView[] = [],
): UniverseCreate {
  return { id: f.id.trim(), ...universeUpdateBody(f, metrics) };
}

/** The definition changed after the last refresh, so the members are behind it. */
export function isStale(u: Pick<UniverseView, 'updated_at' | 'refreshed_at'>): boolean {
  return !!u.refreshed_at && !!u.updated_at && u.updated_at > u.refreshed_at;
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

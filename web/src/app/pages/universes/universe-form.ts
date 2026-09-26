import type { UniverseCreate, UniverseView } from '../../api/models';

export type UniverseKind = UniverseView['kind'];
export type SpecSource = 'spec' | 'csv';

export const KIND_LABEL: Record<UniverseKind, string> = {
  list: 'List',
  exchange: 'Exchange',
  rule: 'Rule',
  index: 'Index',
};

export const KIND_HINT: Record<UniverseKind, string> = {
  list: 'Fixed tickers, or dated spans. Without spans a list has survivorship bias.',
  exchange: 'Every symbol a source lists on an exchange, delisted ones too.',
  rule: 'Filters checked on the lake at each rebalance date.',
  index: 'Index members rebuilt from an imported change history.',
};

/** A starting spec per kind, shown in the spec box (docs/universes.md). */
export const SPEC_TEMPLATES: Record<UniverseKind, object> = {
  list: { tickers: ['AAPL.US', 'MSFT.US'], start_date: '2020-01-01' },
  exchange: { exchange: 'US', source: 'eodhd', include_delisted: true, start_date: '2015-01-01' },
  rule: {
    start: '2020-01-01',
    end: null,
    rebalance: 'monthly',
    min_adv: 1000000,
    min_price: 5,
    asset_classes: ['equity'],
  },
  index: { index_id: 'sp500', source: 'wikipedia_sp500', start_date: '2015-01-01' },
};

export function specTemplate(kind: UniverseKind): string {
  return JSON.stringify(SPEC_TEMPLATES[kind], null, 2);
}

export interface UniverseForm {
  id: string;
  name: string;
  description: string;
  kind: UniverseKind;
  source: SpecSource;
  specText: string;
  csv: string;
}

export type UniverseFormErrors = Partial<Record<'id' | 'spec' | 'csv', string>>;

const ID_PATTERN = /^[a-z0-9][a-z0-9_.-]*$/i;

/** The spec as an object, or the reason it is not one. */
export function parseSpec(text: string): { spec: Record<string, unknown> } | { error: string } {
  if (!text.trim()) return { spec: {} };
  try {
    const value: unknown = JSON.parse(text);
    if (value === null || typeof value !== 'object' || Array.isArray(value)) {
      return { error: 'The spec must be a JSON object, like {"tickers": ["AAPL.US"]}.' };
    }
    return { spec: value as Record<string, unknown> };
  } catch {
    return { error: 'The spec is not valid JSON.' };
  }
}

export function universeFormErrors(f: UniverseForm): UniverseFormErrors {
  const errors: UniverseFormErrors = {};
  const id = f.id.trim();
  if (!id) errors.id = 'Enter an id.';
  else if (!ID_PATTERN.test(id)) errors.id = 'Use letters, digits, dots, dashes or underscores.';
  if (f.source === 'csv') {
    if (!f.csv.trim()) errors.csv = 'Choose a CSV file.';
  } else {
    const parsed = parseSpec(f.specText);
    if ('error' in parsed) errors.spec = parsed.error;
  }
  return errors;
}

/** The request body. CSV only applies to list universes. */
export function universeCreateBody(f: UniverseForm): UniverseCreate {
  const parsed = f.source === 'spec' ? parseSpec(f.specText) : { spec: {} };
  return {
    id: f.id.trim(),
    kind: f.kind,
    name: f.name.trim() || null,
    description: f.description.trim() || null,
    spec: 'spec' in parsed ? parsed.spec : {},
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

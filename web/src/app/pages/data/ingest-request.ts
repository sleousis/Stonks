import type { IngestRequest } from '../../api/models';

export type IngestKind = IngestRequest['kind'];
export type IngestSource = NonNullable<IngestRequest['source']>;

export const INGEST_KINDS: readonly { value: IngestKind; label: string }[] = [
  { value: 'prices', label: 'Daily prices' },
  { value: 'intraday', label: 'Intraday bars' },
  { value: 'fundamentals', label: 'Fundamentals' },
  { value: 'metadata', label: 'Metadata' },
];

/** Raw form state: every field a string, as the inputs hold it. */
export interface IngestFormValue {
  source: IngestSource;
  kind: IngestKind;
  tickers: string;
  exchange: string;
  since: string;
  until: string;
  interval: string;
}

export const EMPTY_INGEST_FORM: IngestFormValue = {
  source: 'eodhd',
  kind: 'prices',
  tickers: '',
  exchange: '',
  since: '',
  until: '',
  interval: '',
};

/** "aapl.us, msft.us" (commas, spaces or new lines) -> ['AAPL.US', 'MSFT.US'], de-duplicated. */
export function parseTickers(text: string): string[] {
  const seen = new Set<string>();
  for (const part of text.split(/[\s,;]+/)) {
    const t = part.trim().toUpperCase();
    if (t) seen.add(t);
  }
  return [...seen];
}

/**
 * The request body, with only the fields that apply: `exchange` for prices,
 * `interval` for intraday, dates only when set.
 */
export function buildIngestRequest(v: IngestFormValue): IngestRequest {
  const request: IngestRequest = {
    source: v.source,
    kind: v.kind,
    tickers: parseTickers(v.tickers),
  };
  const exchange = v.exchange.trim().toUpperCase();
  if (exchange && v.kind === 'prices') request.exchange = exchange;
  if (v.since) request.since = v.since;
  if (v.until) request.until = v.until;
  if (v.kind === 'intraday' && v.interval) request.interval = v.interval;
  return request;
}

/** The API's own validation rules, checked before asking to confirm. */
export function ingestProblems(v: IngestFormValue): string[] {
  const problems: string[] = [];
  const request = buildIngestRequest(v);
  if (request.tickers?.length === 0 && !request.exchange) {
    problems.push(
      v.kind === 'prices'
        ? 'Enter at least one ticker, or an exchange.'
        : 'Enter at least one ticker.',
    );
  }
  if (v.kind === 'intraday' && !v.interval) problems.push('Choose an interval for intraday bars.');
  if (v.since && v.until && v.since > v.until) {
    problems.push('"Since" must be on or before "until".');
  }
  return problems;
}

const MAX_LISTED = 5;

/** One sentence for the confirm dialog: what, for which tickers, from where and when. */
export function describeIngest(r: IngestRequest): string {
  const what = r.kind === 'intraday' ? `${r.interval ?? ''} intraday bars`.trim() : r.kind;
  const tickers = r.tickers ?? [];
  let scope: string;
  if (tickers.length) {
    const listed = tickers.slice(0, MAX_LISTED).join(', ');
    const more = tickers.length > MAX_LISTED ? ` and ${tickers.length - MAX_LISTED} more` : '';
    scope = `${tickers.length} ${tickers.length === 1 ? 'ticker' : 'tickers'} (${listed}${more})`;
  } else {
    scope = `every ticker on ${r.exchange}`;
  }
  const range =
    (r.since ? ` since ${r.since}` : '') + (r.until ? ` until ${r.until}` : '');
  return `Fetches ${what} for ${scope} from ${r.source ?? 'eodhd'}${range} and writes them to the lake.`;
}

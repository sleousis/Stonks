import { humanize } from '../../shared/ui/param-form/param-spec';

/**
 * Trader words for data updates (UX-19, UX-66): the data providers, what an
 * update fetches, and how it ended. Data, Health and Universes all name them
 * through here, so no raw `eodhd`, `prices` or `ok` reaches the screen.
 */
export const SOURCE_LABELS: Readonly<Record<string, string>> = {
  eodhd: 'EODHD',
  yahoo: 'Yahoo Finance',
  defillama: 'DefiLlama',
};

/** "EODHD" for `eodhd`; an unknown provider is humanized. */
export function sourceLabel(id: string | null | undefined): string {
  if (!id) return '';
  // The canned source of a demo install: never "Fake".
  if (id === 'fake') return 'Demo data';
  return SOURCE_LABELS[id] ?? humanize(id);
}

/** Exchange codes our data uses, in words. An unknown code shows as it is. */
export const EXCHANGE_LABELS: Readonly<Record<string, string>> = {
  AS: 'Amsterdam',
  AT: 'Athens',
  BR: 'Brussels',
  CC: 'Crypto',
  COMM: 'Commodities',
  CO: 'Copenhagen',
  F: 'Frankfurt',
  FOREX: 'Currencies',
  HE: 'Helsinki',
  INDX: 'Indices',
  IR: 'Dublin',
  LIS: 'Lisbon',
  LSE: 'London',
  MC: 'Madrid',
  MI: 'Milan',
  OL: 'Oslo',
  PA: 'Paris',
  ST: 'Stockholm',
  SW: 'Swiss',
  TO: 'Toronto',
  VI: 'Vienna',
  XETRA: 'Xetra',
};

/** "Brussels" for `BR`; `US` and unknown codes as they are. */
export function exchangeLabel(code: string | null | undefined): string {
  if (!code) return '';
  return EXCHANGE_LABELS[code.toUpperCase()] ?? code;
}

/** A row's kind: funds read "Fund (ETF)", not "Stocks". */
export function instrumentClassLabel(r: {
  asset_class: string | null | undefined;
  security_type?: string | null;
}): string {
  if (r.security_type === 'etf' || r.security_type === 'fund') return 'Fund (ETF)';
  return assetClassLabel(r.asset_class);
}

/** The intervals most people chart, smallest first. */
const COMMON_INTERVALS = ['1m', '5m', '15m', '1h', '1d', '1w', '1mo'];

const INTERVAL_LABELS: Readonly<Record<string, string>> = {
  '1m': '1 minute',
  '5m': '5 minutes',
  '15m': '15 minutes',
  '1h': '1 hour',
  '1d': 'Daily',
  '1w': 'Weekly',
  '1mo': 'Monthly',
};

/** The common intervals the server knows, plus `current` when it is another one. */
export function commonIntervals(codes: readonly string[], current: string): string[] {
  const out = COMMON_INTERVALS.filter((c) => codes.includes(c));
  if (current && !out.includes(current)) out.push(current);
  return out.length ? out : [...codes];
}

/** "Daily" for `1d`; an uncommon code as it is. */
export function intervalLabel(code: string): string {
  return INTERVAL_LABELS[code] ?? code;
}

export const KIND_LABELS: Readonly<Record<string, string>> = {
  prices: 'Daily prices',
  intraday: 'Intraday bars',
  fundamentals: 'Fundamentals',
  metadata: 'Company details',
  macro: 'Economic data',
  tvl: 'DeFi value locked',
  exchanges: 'Exchange lists',
  aggregate: 'Combined bars',
  borrow: 'Borrow rates',
};

/** "Daily prices" for `prices`; an unknown kind is humanized. */
export function kindLabel(kind: string | null | undefined): string {
  if (!kind) return '';
  return KIND_LABELS[kind] ?? humanize(kind);
}

/** Asset classes in words: "Stocks", never the lower-case id "equity". */
export const ASSET_CLASS_LABELS: Readonly<Record<string, string>> = {
  equity: 'Stocks',
  crypto: 'Crypto',
  commodity: 'Commodities',
  bond: 'Bonds',
};

export function assetClassLabel(cls: string | null | undefined): string {
  if (!cls) return '';
  return ASSET_CLASS_LABELS[cls] ?? humanize(cls);
}

/** How a data update ended, for status pills. */
export const RUN_STATUS_LABELS: Readonly<Record<string, string>> = {
  running: 'Running',
  ok: 'Done',
  partial: 'Partly done',
  error: 'Failed',
};

export function runStatusLabel(status: string | null | undefined): string {
  if (!status) return 'Unknown';
  return RUN_STATUS_LABELS[status] ?? humanize(status);
}

function tickers(n: number): string {
  return `${n} ${n === 1 ? 'ticker' : 'tickers'}`;
}

/** "Updated 2 tickers.", "Updated 1 ticker. 1 ticker failed." or "Could not update 3 tickers." */
export function updateOutcome(r: {
  tickers_ok?: number | null;
  tickers_failed?: number | null;
}): string {
  const ok = r.tickers_ok ?? 0;
  const failed = r.tickers_failed ?? 0;
  if (!failed) return `Updated ${tickers(ok)}.`;
  if (!ok) return `Could not update ${tickers(failed)}.`;
  return `Updated ${tickers(ok)}. ${tickers(failed)} failed.`;
}

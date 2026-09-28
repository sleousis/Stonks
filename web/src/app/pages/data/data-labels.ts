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
  return SOURCE_LABELS[id] ?? humanize(id);
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

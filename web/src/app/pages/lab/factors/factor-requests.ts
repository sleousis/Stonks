import type {
  FactorTearSheetRequest,
  FactorValuesRequest,
  FactorView,
  LabRunRequest,
  WatchlistView,
} from '../../../api/models';
import { type FormErrors, defaultWindow, parseTickers } from '../lab-requests';
import { parseHorizons } from '../research-requests';

/**
 * Form state and request builders for the factor pages: the basket a factor
 * is measured on, values on a date, tear sheets and a factor strategy lab
 * run. Pure, so the payloads are easy to test.
 */

/** The class the `factor` strategy lives in, used when the catalog is not loaded. */
export const FACTOR_STRATEGY_CLASS = 'stonks.strategies.examples.factor_strategy:FactorStrategy';

/** The route segment of the formula workbench (`/lab/factors/formula`). */
export const FORMULA_ROUTE = 'formula';

export type BasketKind = 'universe' | 'watchlist' | 'tickers';

export interface Basket {
  kind: BasketKind;
  universeId: string;
  watchlistId: string;
  tickers: string;
}

export function defaultBasket(): Basket {
  return { kind: 'universe', universeId: '', watchlistId: '', tickers: '' };
}

/** The fields of a request that name its tickers. */
export type BasketFields = Pick<FactorValuesRequest, 'universe' | 'universe_id'>;

/** Why the basket can't be used yet, or null. */
export function basketError(b: Basket, watchlists: readonly WatchlistView[] = []): string | null {
  switch (b.kind) {
    case 'universe':
      return b.universeId ? null : 'Pick a universe.';
    case 'watchlist': {
      if (!b.watchlistId) return 'Pick a watchlist.';
      const list = watchlists.find((w) => w.id === b.watchlistId);
      return list && list.tickers.length === 0 ? 'This watchlist has no tickers.' : null;
    }
    case 'tickers':
      return parseTickers(b.tickers).length ? null : 'Enter at least one ticker.';
  }
}

/** `universe_id` for a stored universe, else the tickers of the watchlist or the typed list. */
export function basketFields(b: Basket, watchlists: readonly WatchlistView[] = []): BasketFields {
  if (b.kind === 'universe') return { universe_id: b.universeId };
  if (b.kind === 'watchlist') {
    const list = watchlists.find((w) => w.id === b.watchlistId);
    return { universe: [...(list?.tickers ?? [])] };
  }
  return { universe: parseTickers(b.tickers) };
}

/** "the sp500 universe", "the Tech watchlist" or "3 tickers", for confirmations. */
export function basketText(b: Basket, watchlists: readonly WatchlistView[] = []): string {
  if (b.kind === 'universe') return `the ${b.universeId} universe`;
  if (b.kind === 'watchlist') {
    const list = watchlists.find((w) => w.id === b.watchlistId);
    return `the ${list?.name ?? b.watchlistId} watchlist`;
  }
  const n = parseTickers(b.tickers).length;
  return `${n} ticker${n === 1 ? '' : 's'}`;
}

// ---- values on a date --------------------------------------------------------

export function buildValuesRequest(
  factor: string,
  asOf: string,
  basket: Basket,
  watchlists: readonly WatchlistView[] = [],
): FactorValuesRequest {
  return { factor, as_of: asOf, ...basketFields(basket, watchlists) };
}

// ---- tear sheets ---------------------------------------------------------------

export interface TearsheetForm {
  basket: Basket;
  start: string;
  end: string;
  /** As typed; blank sends the server's default (1, 5, 21). */
  horizons: string;
  quantiles: number | null;
}

export function defaultTearsheetForm(today?: Date): TearsheetForm {
  const { end } = defaultWindow(today);
  const start = `${Number(end.slice(0, 4)) - 3}${end.slice(4)}`;
  return { basket: defaultBasket(), start, end, horizons: '1, 5, 21', quantiles: 5 };
}

export function tearsheetErrors(
  f: TearsheetForm,
  watchlists: readonly WatchlistView[] = [],
): FormErrors {
  const e: FormErrors = {};
  const basket = basketError(f.basket, watchlists);
  if (basket) e['basket'] = basket;
  if (!f.start) e['start'] = 'Pick a start date.';
  if (!f.end) e['end'] = 'Pick an end date.';
  if (f.start && f.end && f.start >= f.end) e['end'] = 'End must be after start.';
  if (f.horizons.trim()) {
    const h = parseHorizons(f.horizons);
    if (!h) e['horizons'] = 'Whole numbers of bars from 1 to 504, e.g. 1, 5, 21.';
    else if (h.length > 20) e['horizons'] = 'At most 20 horizons.';
  }
  const q = f.quantiles;
  if (q === null || !Number.isInteger(q) || q < 2 || q > 20) e['quantiles'] = 'From 2 to 20.';
  return e;
}

export function buildTearsheetRequest(
  factor: string,
  f: TearsheetForm,
  watchlists: readonly WatchlistView[] = [],
): FactorTearSheetRequest {
  const body: FactorTearSheetRequest = {
    factor,
    start: f.start,
    end: f.end,
    n_quantiles: f.quantiles ?? 5,
    ...basketFields(f.basket, watchlists),
  };
  const horizons = f.horizons.trim() ? parseHorizons(f.horizons) : null;
  if (horizons?.length) body.horizons = horizons;
  return body;
}

// ---- a factor strategy lab run ---------------------------------------------------

export type FactorSuite = 'quick' | 'standard' | 'promotion';

export interface FactorRunForm {
  basket: Basket;
  start: string;
  end: string;
  suite: FactorSuite;
  budget: number | null;
  hypothesis: string;
}

/** A library factor's own hypothesis starts the form; a formula starts blank (P1). */
export function defaultFactorRunForm(factor: FactorView | null, today?: Date): FactorRunForm {
  const { end } = defaultWindow(today);
  const start = `${Number(end.slice(0, 4)) - 5}${end.slice(4)}`;
  return {
    basket: defaultBasket(),
    start,
    end,
    suite: 'quick',
    budget: 20,
    hypothesis: factor?.hypothesis ?? '',
  };
}

export function factorRunErrors(
  f: FactorRunForm,
  watchlists: readonly WatchlistView[] = [],
): FormErrors {
  const e: FormErrors = {};
  const basket = basketError(f.basket, watchlists);
  if (basket) e['basket'] = basket;
  if (!f.start) e['start'] = 'Pick a start date.';
  if (!f.end) e['end'] = 'Pick an end date.';
  if (f.start && f.end && f.start >= f.end) e['end'] = 'End must be after start.';
  const b = f.budget;
  if (b === null || !Number.isInteger(b) || b < 1 || b > 1000) e['budget'] = 'From 1 to 1000.';
  if (f.hypothesis.trim().length < 10)
    e['hypothesis'] = 'Say why this factor should rank future returns before you test it.';
  if (f.hypothesis.length > 4000) e['hypothesis'] = 'At most 4000 characters.';
  return e;
}

/**
 * The lab run for the `factor` strategy: the factor (an id or a formula)
 * stays fixed while the tuner searches the slice of names held.
 */
export function buildFactorRunRequest(
  factor: string,
  classPath: string,
  f: FactorRunForm,
  watchlists: readonly WatchlistView[] = [],
): LabRunRequest {
  const body: LabRunRequest = {
    strategy: { class_path: classPath, params: { factor } },
    start: f.start,
    end: f.end,
    interval: '1d',
    tuner: 'random',
    budget: f.budget ?? 20,
    objective: 'sharpe',
    preset: f.suite,
    hypothesis: f.hypothesis.trim(),
  };
  const fields = basketFields(f.basket, watchlists);
  if (fields.universe_id) body.universe_id = fields.universe_id;
  else body.universe = fields.universe ?? [];
  return body;
}

// ---- display helpers -------------------------------------------------------------

/** "+1: higher is better" or "-1: lower is better". */
export function directionText(direction: number): string {
  return direction < 0 ? 'Lower is better' : 'Higher is better';
}

/** "252 bars" of warm-up, or "none". */
export function warmupText(bars: number | null | undefined): string {
  if (bars == null) return 'n/a';
  if (bars === 0) return 'None';
  return `${bars} ${bars === 1 ? 'bar' : 'bars'}`;
}

/** Text search over a factor's id, set, family and description. */
export function matchesQuery(f: FactorView, query: string): boolean {
  const q = query.trim().toLowerCase();
  if (!q) return true;
  return `${f.id} ${f.set ?? ''} ${f.family} ${f.description} ${f.expression ?? ''}`
    .toLowerCase()
    .includes(q);
}

/**
 * A cell colour for a signed value on a diverging scale: loss below 0, gain
 * above, the surface at 0. `scale` is the magnitude that gets the full colour.
 */
export function divergingColor(value: number | null | undefined, scale: number): string | null {
  if (value == null || !Number.isFinite(value) || scale <= 0) return null;
  const share = Math.round(Math.min(1, Math.abs(value) / scale) * 60);
  const tone = value < 0 ? 'var(--color-loss)' : 'var(--color-gain)';
  return `color-mix(in srgb, ${tone} ${share}%, var(--color-surface))`;
}

/** The largest finite magnitude, for `divergingColor`'s scale. */
export function maxMagnitude(values: readonly (number | null | undefined)[]): number {
  let max = 0;
  for (const v of values) if (v != null && Number.isFinite(v)) max = Math.max(max, Math.abs(v));
  return max;
}

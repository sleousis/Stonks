import type { SignalIcRequest, SweepRequest } from '../../api/models';
import {
  type FormErrors,
  type SuitePreset,
  type WindowForm,
  defaultWindow,
  parseTickers,
  windowErrors,
} from './lab-requests';

/**
 * Form state and request builders for the Lab's research screens: sweeps
 * (every strategy through the lab on one basket) and signal IC. Pure, so
 * the payloads are easy to test.
 */

export type BasketChoice = 'tickers' | 'universe';

export interface SweepForm {
  basket: BasketChoice;
  tickers: string;
  universeId: string;
  start: string;
  end: string;
  interval: string;
  /** Class paths to run; empty runs every catalogued strategy. */
  strategies: readonly string[];
  suite: SuitePreset;
  tuner: 'grid' | 'random';
  budget: number | null;
  objective: NonNullable<SweepRequest['objective']>;
}

export function defaultSweepForm(today?: Date): SweepForm {
  return {
    basket: 'tickers',
    tickers: '',
    universeId: '',
    ...defaultWindow(today),
    interval: '1d',
    strategies: [],
    suite: 'quick',
    tuner: 'random',
    budget: 20,
    objective: 'sharpe',
  };
}

function dateErrors(f: Pick<WindowForm, 'start' | 'end'>): FormErrors {
  const e: FormErrors = {};
  if (!f.start) e['start'] = 'Pick a start date.';
  if (!f.end) e['end'] = 'Pick an end date.';
  if (f.start && f.end && f.start >= f.end) e['end'] = 'End must be after start.';
  return e;
}

export function sweepErrors(f: SweepForm): FormErrors {
  const e = dateErrors(f);
  if (f.basket === 'tickers' && parseTickers(f.tickers).length === 0)
    e['tickers'] = 'Enter at least one ticker.';
  if (f.basket === 'universe' && !f.universeId) e['universe'] = 'Pick a universe.';
  if (f.budget === null || !Number.isInteger(f.budget) || f.budget < 1 || f.budget > 1000)
    e['budget'] = 'Between 1 and 1000.';
  return e;
}

export function buildSweepRequest(f: SweepForm): SweepRequest {
  const body: SweepRequest = {
    start: f.start,
    end: f.end,
    interval: f.interval,
    preset: f.suite,
    tuner: f.tuner,
    budget: f.budget ?? 20,
    objective: f.objective,
  };
  if (f.basket === 'universe') body.universe_id = f.universeId;
  else body.universe = parseTickers(f.tickers);
  if (f.strategies.length) body.strategies = [...f.strategies];
  return body;
}

export interface SignalIcForm extends WindowForm {
  /** Forward horizons in bars, as typed; blank uses the default set. */
  horizons: string;
}

export function defaultSignalIcForm(today?: Date): SignalIcForm {
  return { classPath: '', tickers: '', ...defaultWindow(today), interval: '1d', horizons: '' };
}

/** The server's longest horizon. */
export const MAX_HORIZON_BARS = 504;

/** "1, 5 21" → [1, 5, 21]; `null` when something is not a whole number in range. */
export function parseHorizons(text: string): number[] | null {
  const parts = text.split(/[\s,;]+/).filter(Boolean);
  const nums = parts.map(Number);
  if (nums.some((n) => !Number.isInteger(n) || n < 1 || n > MAX_HORIZON_BARS)) return null;
  return [...new Set(nums)].sort((a, b) => a - b);
}

export function signalIcErrors(f: SignalIcForm): FormErrors {
  const e = windowErrors(f);
  if (f.horizons.trim()) {
    const h = parseHorizons(f.horizons);
    if (!h) e['horizons'] = `Whole numbers of bars from 1 to ${MAX_HORIZON_BARS}, e.g. 1, 5, 21.`;
    else if (h.length > 20) e['horizons'] = 'At most 20 horizons.';
  }
  return e;
}

export function buildSignalIcRequest(f: SignalIcForm): SignalIcRequest {
  const body: SignalIcRequest = {
    strategy: { class_path: f.classPath },
    universe: parseTickers(f.tickers),
    start: f.start,
    end: f.end,
    interval: f.interval,
  };
  const horizons = f.horizons.trim() ? parseHorizons(f.horizons) : null;
  if (horizons?.length) body.horizons = horizons;
  return body;
}

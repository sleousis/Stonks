import { Injectable, computed, signal } from '@angular/core';

import { type ActiveFormat, type DateStyle, activeFormat, browserFormat } from './format';

export const FORMAT_STORAGE_KEY = 'stonks.format';

/** The user's choice; `null` means "use the browser's". */
export interface FormatPrefs {
  locale: string | null;
  timeZone: string | null;
  dateStyle: DateStyle;
}

const DEFAULT_PREFS: FormatPrefs = { locale: null, timeZone: null, dateStyle: 'iso' };

/** Locales offered in Settings. Any valid BCP 47 tag works if stored. */
export const LOCALE_OPTIONS: readonly { value: string; label: string }[] = [
  { value: 'en-US', label: 'English (United States)' },
  { value: 'en-GB', label: 'English (United Kingdom)' },
  { value: 'en-IE', label: 'English (Ireland)' },
  { value: 'en-IN', label: 'English (India)' },
  { value: 'de-DE', label: 'Deutsch (Deutschland)' },
  { value: 'de-CH', label: 'Deutsch (Schweiz)' },
  { value: 'fr-FR', label: 'Français (France)' },
  { value: 'es-ES', label: 'Español (España)' },
  { value: 'it-IT', label: 'Italiano (Italia)' },
  { value: 'nl-NL', label: 'Nederlands (Nederland)' },
  { value: 'el-GR', label: 'Ελληνικά (Ελλάδα)' },
  { value: 'ja-JP', label: '日本語 (日本)' },
];

/**
 * The one place that decides how numbers, money, percentages and dates look:
 * locale, time zone and date style. The choice is a per-browser convenience
 * kept in localStorage; it feeds the `activeFormat` signal every formatter in
 * format.ts reads, so figures re-render as soon as it changes.
 *
 * Instantiated at startup (app.config) so the stored choice applies before
 * the first page renders.
 */
@Injectable({ providedIn: 'root' })
export class FormatService {
  private readonly state = signal<FormatPrefs>(readPrefs());

  readonly prefs = this.state.asReadonly();
  readonly active = computed<ActiveFormat>(() => resolve(this.state()));
  /** The browser's defaults, for "Browser default (…)" labels. */
  readonly browser = browserFormat();

  constructor() {
    activeFormat.set(this.active());
  }

  update(change: Partial<FormatPrefs>): void {
    const next = sanitize({ ...this.state(), ...change });
    this.state.set(next);
    activeFormat.set(resolve(next));
    try {
      if (isDefault(next)) localStorage.removeItem(FORMAT_STORAGE_KEY);
      else localStorage.setItem(FORMAT_STORAGE_KEY, JSON.stringify(next));
    } catch {
      // Storage blocked: the choice still applies for this page view.
    }
  }

  reset(): void {
    this.update(DEFAULT_PREFS);
  }

  /** IANA zones the browser knows, with the browser's own zone and UTC. */
  timeZones(): string[] {
    const intl = Intl as typeof Intl & { supportedValuesOf?: (key: string) => string[] };
    const zones = new Set<string>(intl.supportedValuesOf?.('timeZone') ?? []);
    zones.add(this.browser.timeZone);
    zones.add('UTC');
    return [...zones].sort();
  }
}

function resolve(prefs: FormatPrefs): ActiveFormat {
  const browser = browserFormat();
  return {
    locale: prefs.locale ?? browser.locale,
    timeZone: prefs.timeZone ?? browser.timeZone,
    dateStyle: prefs.dateStyle,
  };
}

function isDefault(p: FormatPrefs): boolean {
  return p.locale === null && p.timeZone === null && p.dateStyle === 'iso';
}

function readPrefs(): FormatPrefs {
  try {
    const raw = localStorage.getItem(FORMAT_STORAGE_KEY);
    return raw ? sanitize(JSON.parse(raw) as Partial<FormatPrefs>) : DEFAULT_PREFS;
  } catch {
    return DEFAULT_PREFS;
  }
}

function sanitize(p: Partial<FormatPrefs>): FormatPrefs {
  return {
    locale: validLocale(p.locale) ? p.locale : null,
    timeZone: validTimeZone(p.timeZone) ? p.timeZone : null,
    dateStyle: p.dateStyle === 'locale' ? 'locale' : 'iso',
  };
}

export function validLocale(value: unknown): value is string {
  if (typeof value !== 'string' || !value) return false;
  try {
    return Intl.NumberFormat.supportedLocalesOf([value]).length > 0;
  } catch {
    return false;
  }
}

export function validTimeZone(value: unknown): value is string {
  if (typeof value !== 'string' || !value) return false;
  try {
    new Intl.DateTimeFormat('en-US', { timeZone: value });
    return true;
  } catch {
    return false;
  }
}

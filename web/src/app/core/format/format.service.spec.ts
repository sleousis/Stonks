import { TestBed } from '@angular/core/testing';

import {
  activeFormat,
  browserFormat,
  formatDate,
  formatDateTime,
  formatMoney,
  formatNumber,
  formatPercent,
} from './format';
import { FORMAT_STORAGE_KEY, FormatService } from './format.service';

describe('FormatService', () => {
  beforeEach(() => {
    localStorage.clear();
    activeFormat.set(browserFormat());
  });
  afterEach(() => {
    localStorage.clear();
    activeFormat.set(browserFormat());
  });

  function create(): FormatService {
    TestBed.configureTestingModule({});
    return TestBed.inject(FormatService);
  }

  it('defaults to the browser locale and zone with ISO dates', () => {
    const svc = create();
    expect(svc.prefs()).toEqual({ locale: null, timeZone: null, dateStyle: 'iso' });
    expect(activeFormat().locale).toBe(browserFormat().locale);
    expect(activeFormat().timeZone).toBe(browserFormat().timeZone);
  });

  it('formats numbers, money and percent for the chosen locale', () => {
    const svc = create();
    svc.update({ locale: 'de-DE' });
    expect(formatNumber(1234.5)).toBe('1.234,5');
    expect(formatMoney(1234.5)).toMatch(/^1\.234,50\s\$$/);
    expect(formatPercent(0.0123)).toMatch(/^1,23\s%$/);
    expect(formatPercent(0.0123, { signed: true })).toMatch(/^\+1,23\s%$/);

    svc.update({ locale: 'en-US' });
    expect(formatMoney(-1234.5)).toBe('-$1,234.50');
    expect(formatPercent(0.0123)).toBe('1.23%');
  });

  it('shows date-times in the chosen time zone', () => {
    const svc = create();
    svc.update({ locale: 'en-US', timeZone: 'America/New_York' });
    expect(formatDateTime('2026-09-26T14:05:00Z')).toBe('2026-09-26 10:05');
    svc.update({ timeZone: 'Asia/Tokyo' });
    expect(formatDateTime('2026-09-26T20:05:00Z')).toBe('2026-09-27 05:05');
    expect(formatDate('2026-09-26T20:05:00Z')).toBe('2026-09-27');
  });

  it('never shifts date-only values, in either date style', () => {
    const svc = create();
    svc.update({ locale: 'en-US', timeZone: 'Pacific/Honolulu' });
    expect(formatDate('2026-09-26')).toBe('2026-09-26');
    svc.update({ dateStyle: 'locale' });
    expect(formatDate('2026-09-26')).toBe('Sep 26, 2026');
    svc.update({ locale: 'en-GB' });
    expect(formatDate('2026-09-26')).toBe('26 Sept 2026');
  });

  it('remembers the choice in localStorage and restores it', () => {
    const svc = create();
    svc.update({ locale: 'fr-FR', timeZone: 'Europe/Paris', dateStyle: 'locale' });
    expect(JSON.parse(localStorage.getItem(FORMAT_STORAGE_KEY) ?? '{}')).toEqual({
      locale: 'fr-FR',
      timeZone: 'Europe/Paris',
      dateStyle: 'locale',
    });

    TestBed.resetTestingModule();
    activeFormat.set(browserFormat());
    const again = create();
    expect(again.prefs().locale).toBe('fr-FR');
    expect(activeFormat()).toEqual({
      locale: 'fr-FR',
      timeZone: 'Europe/Paris',
      dateStyle: 'locale',
    });
  });

  it('ignores an invalid stored locale or time zone', () => {
    localStorage.setItem(
      FORMAT_STORAGE_KEY,
      JSON.stringify({ locale: 'not a locale!!', timeZone: 'Mars/Olympus', dateStyle: 'x' }),
    );
    const svc = create();
    expect(svc.prefs()).toEqual({ locale: null, timeZone: null, dateStyle: 'iso' });
  });

  it('resets to the browser defaults', () => {
    const svc = create();
    svc.update({ locale: 'de-DE' });
    svc.reset();
    expect(svc.prefs().locale).toBeNull();
    expect(localStorage.getItem(FORMAT_STORAGE_KEY)).toBeNull();
    expect(activeFormat().locale).toBe(browserFormat().locale);
  });

  it('lists time zones including the browser zone', () => {
    const svc = create();
    expect(svc.timeZones()).toContain(browserFormat().timeZone);
    expect(svc.timeZones()).toContain('UTC');
  });
});

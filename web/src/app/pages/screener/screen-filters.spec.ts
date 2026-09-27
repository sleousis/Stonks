import { Component, signal } from '@angular/core';
import { TestBed } from '@angular/core/testing';

import type { MetricView } from '../../api/models';
import { METRICS } from '../../../testing/screener-fixtures';
import { ScreenFilters } from './screen-filters';
import { type FilterRow, MAX_FILTERS, filterRow } from './screen-form';

@Component({
  imports: [ScreenFilters],
  template: `<app-screen-filters idPrefix="t" [metrics]="metrics" [(filters)]="filters" />`,
})
class Host {
  metrics: MetricView[] = METRICS;
  readonly filters = signal<FilterRow[]>([]);
}

describe('ScreenFilters', () => {
  function render() {
    const fixture = TestBed.createComponent(Host);
    fixture.detectChanges();
    const el = fixture.nativeElement as HTMLElement;
    const button = (text: string) =>
      [...el.querySelectorAll('button')].find((b) => b.textContent?.trim() === text)!;
    return { fixture, el, button, host: fixture.componentInstance };
  }

  it('adds, edits and removes filter rows through the two-way binding', () => {
    const { fixture, el, button, host } = render();
    expect(el.textContent).toContain('No filters yet');

    button('Add a filter').click();
    fixture.detectChanges();
    expect(host.filters()).toHaveLength(1);
    const select = el.querySelector<HTMLSelectElement>('select[id^="t-f-metric-"]')!;
    // Metrics come grouped by price and fundamentals.
    expect([...select.querySelectorAll('optgroup')].map((g) => g.label)).toEqual([
      'Price',
      'Fundamentals',
    ]);
    select.value = 'dividend_yield';
    select.dispatchEvent(new Event('change'));
    const min = el.querySelector<HTMLInputElement>('input[id^="t-f-min-"]')!;
    min.value = '3';
    min.dispatchEvent(new Event('input'));
    fixture.detectChanges();
    expect(host.filters()[0]).toMatchObject({ metric: 'dividend_yield', min: '3', max: '' });
    expect(el.textContent).toContain('In percent: 8 means 8%.');

    el.querySelector<HTMLButtonElement>('button[aria-label="Remove filter 1"]')!.click();
    fixture.detectChanges();
    expect(host.filters()).toEqual([]);
  });

  it('stops adding at the API limit', () => {
    const { fixture, button, host } = render();
    host.filters.set(Array.from({ length: MAX_FILTERS }, () => filterRow('price', '1')));
    fixture.detectChanges();
    expect(button('Add a filter').disabled).toBe(true);
  });
});

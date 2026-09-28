import { TestBed } from '@angular/core/testing';

import { MonthlyReturns, strength, yearRows } from './monthly-returns';

const MONTHS = [
  { month: '2025-12', value: -0.06 },
  { month: '2026-01', value: 0.01 },
  { month: '2026-02', value: 0.03 },
  { month: '2026-03', value: null },
];

describe('monthly returns', () => {
  it('shades cells by size as well as sign', () => {
    expect([strength(null), strength(0), strength(0.01), strength(-0.03), strength(0.08)]).toEqual([
      0, 0, 1, 2, 3,
    ]);
    const [y2026, y2025] = yearRows(MONTHS);
    expect(y2026.cells.slice(0, 3).map((c) => [c.text, c.tone, c.strength])).toEqual([
      ['+1.0%', 'gain', 1],
      ['+3.0%', 'gain', 2],
      ['', '', 0],
    ]);
    expect(y2026.total).toBe('+4.0%');
    expect(y2025.cells[11]).toMatchObject({ tone: 'loss', strength: 3 });
  });

  it('draws a captioned table, newest year first, with the percent in every cell', () => {
    const fixture = TestBed.createComponent(MonthlyReturns);
    fixture.componentRef.setInput('months', MONTHS);
    fixture.componentRef.setInput('caption', 'Return by month');
    fixture.detectChanges();
    const el: HTMLElement = fixture.nativeElement;
    expect(el.querySelector('caption')?.textContent?.trim()).toBe('Return by month');
    const rows = [...el.querySelectorAll('tbody tr')];
    expect(rows.map((r) => r.querySelector('th')?.textContent)).toEqual(['2026', '2025']);
    const dec = rows[1].querySelectorAll('td')[11];
    expect(dec.textContent?.trim()).toBe('-6.0%');
    expect(dec.getAttribute('data-strength')).toBe('3');
    // the scroller is reachable by keyboard on a phone
    expect(el.querySelector('.months-scroll')?.getAttribute('tabindex')).toBe('0');
  });

  it('names the last column Year total, not a second Year', () => {
    const fixture = TestBed.createComponent(MonthlyReturns);
    fixture.componentRef.setInput('months', MONTHS);
    fixture.componentRef.setInput('caption', 'Return by month');
    fixture.detectChanges();
    const heads = [...(fixture.nativeElement as HTMLElement).querySelectorAll('thead th')];
    expect(heads.map((h) => h.textContent?.trim()).filter((t) => t?.startsWith('Year'))).toEqual([
      'Year',
      'Year total',
    ]);
  });

  it('stacks by year with only the months that have a return (never hides the only one)', () => {
    const [only] = yearRows([{ month: '2026-09', value: 0.012 }]);
    expect(only.span.map((c) => [c.month, c.text])).toEqual([['Sep', '+1.2%']]);
    const [y2026] = yearRows(MONTHS);
    // Jan and Feb have returns; Mar is empty and trails, so it is left out.
    expect(y2026.span.map((c) => c.month)).toEqual(['Jan', 'Feb']);

    const fixture = TestBed.createComponent(MonthlyReturns);
    fixture.componentRef.setInput('months', [{ month: '2026-09', value: 0.012 }]);
    fixture.componentRef.setInput('caption', 'Return by month');
    fixture.detectChanges();
    const stacked = (fixture.nativeElement as HTMLElement).querySelector('.stacked')!;
    expect(stacked.querySelector('.year-head')?.textContent).toContain('Year total');
    expect([...stacked.querySelectorAll('dt')].map((d) => d.textContent)).toEqual(['Sep']);
    expect(stacked.querySelector('dd')?.textContent?.trim()).toBe('+1.2%');
  });
});

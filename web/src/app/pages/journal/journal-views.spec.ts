import { HttpTestingController, provideHttpClientTesting } from '@angular/common/http/testing';
import { type ComponentFixture, TestBed } from '@angular/core/testing';
import type { Type } from '@angular/core';
import { provideRouter } from '@angular/router';

import { provideApi } from '../../api/provide-api';
import { nextRequest, tick } from '../../../testing/http';
import { JournalCalendar } from './journal-calendar';
import { JournalResults } from './journal-results';
import { JournalTrades } from './journal-trades';
import { bucket, calendar, group, leg } from './journal.fixtures';

async function settle(fixture: ComponentFixture<unknown>): Promise<void> {
  for (let i = 0; i < 4; i++) {
    await tick(5);
    fixture.detectChanges();
  }
}

function setup<T>(component: Type<T>) {
  TestBed.configureTestingModule({
    imports: [component],
    providers: [...provideApi(), provideHttpClientTesting(), provideRouter([])],
  });
  const controller = TestBed.inject(HttpTestingController);
  const fixture = TestBed.createComponent(component);
  fixture.detectChanges();
  return { controller, fixture, el: fixture.nativeElement as HTMLElement };
}

describe('JournalTrades', () => {
  it('lists trades with R, each linking to its review', async () => {
    const { controller, fixture, el } = setup(JournalTrades);
    const req = await nextRequest(controller, '/api/journal/trades');
    expect(req.request.urlWithParams).toContain('status=all');
    req.flush({ items: [leg()], total: 1, limit: 25, offset: 0 });
    await settle(fixture);
    const link = el.querySelector<HTMLAnchorElement>('table a')!;
    expect(link.getAttribute('href')).toBe('/journal/trades/7');
    const row = el.querySelector('tbody tr')!.textContent ?? '';
    expect(row).toContain('+2.0R');
    expect(row).toContain('By hand');
    expect(row).toContain('3 days');
    // Excursions and exit efficiency live on the trade review page.
    const heads = [...el.querySelectorAll('thead th')].map((h) => h.textContent?.trim() ?? '');
    expect(heads.some((h) => h.includes('Worst move'))).toBe(false);
    expect(heads.some((h) => h.includes('Exit efficiency'))).toBe(false);
    expect(el.textContent).not.toContain('Excursions');
    controller.verify();
  });

  it('asks for open trades when the filter changes', async () => {
    const { controller, fixture, el } = setup(JournalTrades);
    (await nextRequest(controller, '/api/journal/trades')).flush({
      items: [],
      total: 0,
      limit: 25,
      offset: 0,
    });
    await settle(fixture);
    expect(el.textContent).toContain('No trades yet');
    const open = Array.from(el.querySelectorAll<HTMLButtonElement>('[role=radio]')).find(
      (b) => b.textContent?.trim() === 'Open',
    )!;
    open.click();
    fixture.detectChanges();
    const req = await nextRequest(controller, '/api/journal/trades');
    expect(req.request.urlWithParams).toContain('status=open');
    req.flush({
      items: [leg({ is_open: true, r_multiple: null, exit_trigger: null })],
      total: 1,
      limit: 25,
      offset: 0,
    });
    await settle(fixture);
    // Open trades have no R or exit yet: those columns go.
    const heads = [...el.querySelectorAll('thead th')].map((h) => h.textContent?.trim() ?? '');
    expect(heads.some((h) => h === 'R' || h.startsWith('R '))).toBe(false);
    expect(heads.some((h) => h.startsWith('Exit'))).toBe(false);
    controller.verify();
  });
});

describe('JournalCalendar', () => {
  it('draws the month with day and week totals', async () => {
    const { controller, fixture, el } = setup(JournalCalendar);
    (fixture.componentInstance as JournalCalendar).month.set('2026-03');
    (await nextRequest(controller, '/api/journal/calendar')).flush(calendar());
    await settle(fixture);
    const table = el.querySelector('table.cal')!;
    expect(table.querySelectorAll('tbody tr').length).toBe(6);
    expect(table.textContent).toContain('+$100.00');
    expect(el.textContent).toContain('By month');
    expect(el.querySelector('caption')?.textContent).toContain('March 2026');
    expect(el.textContent).toContain('1 trade closed');
    expect(el.textContent).not.toContain('1 trades');
    controller.verify();
  });

  it('takes the best and worst day from the month shown', async () => {
    const { controller, fixture, el } = setup(JournalCalendar);
    (fixture.componentInstance as JournalCalendar).month.set('2026-04');
    (await nextRequest(controller, '/api/journal/calendar')).flush(
      calendar({
        days: [bucket('2026-03-05', 100), bucket('2026-04-02', 30), bucket('2026-04-09', -20)],
        months: [bucket('2026-03', 100), bucket('2026-04', 10, 2)],
        trades: 3,
      }),
    );
    await settle(fixture);
    const tiles = el.querySelector('.tiles')!.textContent ?? '';
    expect(tiles).toContain('+$30.00');
    expect(tiles).toContain('-$20.00');
    expect(tiles).not.toContain('2026-03-05');
    controller.verify();
  });

  it('says so before the first closed trade', async () => {
    const { controller, fixture, el } = setup(JournalCalendar);
    (await nextRequest(controller, '/api/journal/calendar')).flush(
      calendar({ days: [], weeks: [], months: [], total: 0, trades: 0 }),
    );
    await settle(fixture);
    expect(el.textContent).toContain('No closed trades yet');
    expect(el.querySelector('table')).toBeNull();
    controller.verify();
  });
});

describe('JournalResults', () => {
  it('groups by plan first and names groups in plain words', async () => {
    const { controller, fixture, el } = setup(JournalResults);
    const req = await nextRequest(controller, '/api/journal/breakdown');
    expect(req.request.urlWithParams).toContain('by=plan');
    req.flush({
      portfolio_id: 'pf_default',
      by: 'plan',
      base_currency: 'USD',
      since: null,
      until: null,
      groups: [group({ profit_factor: 2.8466 })],
      unconverted: 0,
      fx_missing: [],
    });
    await settle(fixture);
    const text = el.querySelector('table')?.textContent ?? '';
    expect(text).toContain('Broke the plan');
    expect(text).toContain('+1.5R');
    expect(text).toContain('50.0%');
    expect(text).toContain('2.85');
    expect(text).not.toContain('2.8466');
    const heads = [...el.querySelectorAll('thead th')].map((h) => h.textContent?.trim() ?? '');
    expect(heads.some((h) => h.startsWith('Open'))).toBe(false);
    controller.verify();
  });

  it('groups by another label from the filter pills', async () => {
    const { controller, fixture, el } = setup(JournalResults);
    (await nextRequest(controller, '/api/journal/breakdown')).flush({
      portfolio_id: 'pf_default',
      by: 'plan',
      base_currency: 'USD',
      since: null,
      until: null,
      groups: [],
      unconverted: 0,
      fx_missing: [],
    });
    await settle(fixture);
    const pills = [...el.querySelectorAll<HTMLButtonElement>('.filter-pill')];
    expect(pills.find((p) => p.getAttribute('aria-pressed') === 'true')?.textContent).toContain(
      'Plan',
    );
    pills.find((p) => p.textContent!.trim() === 'Tag')!.click();
    await settle(fixture);
    const req = await nextRequest(controller, '/api/journal/breakdown');
    expect(req.request.urlWithParams).toContain('by=tag');
    req.flush({
      portfolio_id: 'pf_default',
      by: 'tag',
      base_currency: 'USD',
      since: null,
      until: null,
      groups: [],
      unconverted: 0,
      fx_missing: [],
    });
    controller.verify();
  });
});

import { HttpTestingController, provideHttpClientTesting } from '@angular/common/http/testing';
import { type ComponentFixture, TestBed } from '@angular/core/testing';
import type { Type } from '@angular/core';
import { provideRouter } from '@angular/router';

import { provideApi } from '../../api/provide-api';
import { nextRequest, tick } from '../../../testing/http';
import { JournalCalendar } from './journal-calendar';
import { JournalResults } from './journal-results';
import { JournalTrades } from './journal-trades';
import { calendar, group, leg } from './journal.fixtures';

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
  it('lists trades with R and excursions, each linking to its review', async () => {
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
    expect(row).toContain('75.0%');
    expect(row).toContain('3 days');
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
    req.flush({ items: [], total: 0, limit: 25, offset: 0 });
    await settle(fixture);
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
      groups: [group()],
      unconverted: 0,
      fx_missing: [],
    });
    await settle(fixture);
    const text = el.querySelector('table')?.textContent ?? '';
    expect(text).toContain('Broke the plan');
    expect(text).toContain('+1.5R');
    controller.verify();
  });
});

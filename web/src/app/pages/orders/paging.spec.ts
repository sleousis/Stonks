import { HttpTestingController, provideHttpClientTesting } from '@angular/common/http/testing';
import { NO_ERRORS_SCHEMA, type Type } from '@angular/core';
import { type ComponentFixture, TestBed } from '@angular/core/testing';
import { provideRouter } from '@angular/router';

import { provideApi } from '../../api/provide-api';
import { nextRequest, tick } from '../../../testing/http';
import { OrdersListPage } from './orders-list.page';
import { TickRunner } from './tick-runner';
import { TicksPage } from './ticks.page';

/**
 * Server paging on the orders pages: each load re-creates the table, so the
 * pager must read its page from the API page's offset (UI-03).
 */
async function pageThrough<T>(
  component: Type<T>,
  path: string,
  size: number,
  row: (i: number) => object,
): Promise<{ fixture: ComponentFixture<T>; offsets: string[] }> {
  const http = TestBed.inject(HttpTestingController);
  const fixture = TestBed.createComponent(component);
  const el = fixture.nativeElement as HTMLElement;
  const offsets: string[] = [];
  const settle = async () => {
    for (let i = 0; i < 3; i++) {
      await tick();
      fixture.detectChanges();
    }
  };
  const serve = async () => {
    const req = await nextRequest(http, path);
    const offset = Number(/offset=(\d+)/.exec(req.request.urlWithParams)?.[1] ?? 0);
    offsets.push(String(offset));
    const items = Array.from({ length: size }, (_, i) => row(offset + i));
    req.flush({ items, total: size * 5, limit: size, offset });
    await settle();
  };
  const next = () =>
    [...el.querySelectorAll<HTMLButtonElement>('.pager button')]
      .find((b) => b.textContent?.trim() === 'Next')!
      .click();

  fixture.detectChanges();
  await serve();
  next();
  fixture.detectChanges();
  await serve();
  next();
  fixture.detectChanges();
  await serve();
  http.verify();
  return { fixture, offsets };
}

function range(fixture: ComponentFixture<unknown>): string {
  return (fixture.nativeElement as HTMLElement).querySelector('.pager .range')!.textContent!.trim();
}

describe('server paging on the orders pages', () => {
  beforeEach(() => {
    TestBed.configureTestingModule({
      providers: [...provideApi(), provideHttpClientTesting(), provideRouter([])],
    });
  });

  it('orders: Next twice loads offset 50 then 100 and the range reads 101–150', async () => {
    const { fixture, offsets } = await pageThrough(OrdersListPage, '/api/orders', 50, (i) => ({
      client_id: `c${i}`,
      ticker: 'AAPL.US',
      side: 'buy',
      quantity: 1,
      status: 'filled',
      created_at: '2026-09-25T20:45:00Z',
    }));
    expect(offsets).toEqual(['0', '50', '100']);
    expect(range(fixture)).toBe('101–150 of 250');
  });

  it('ticks: Next twice loads offset 25 then 50 and the range reads 51–75', async () => {
    TestBed.overrideComponent(TicksPage, {
      remove: { imports: [TickRunner] },
      add: { schemas: [NO_ERRORS_SCHEMA] },
    });
    const { fixture, offsets } = await pageThrough(TicksPage, '/api/ticks', 25, (i) => ({
      id: `t${i}`,
      status: 'ok',
      started_at: '2026-09-25T20:45:00Z',
      finished_at: '2026-09-25T20:46:00Z',
      summary: null,
    }));
    expect(offsets).toEqual(['0', '25', '50']);
    expect(range(fixture)).toBe('51–75 of 125');
  });
});

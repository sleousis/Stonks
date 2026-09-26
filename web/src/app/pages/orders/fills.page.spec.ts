import { HttpTestingController, provideHttpClientTesting } from '@angular/common/http/testing';
import { TestBed } from '@angular/core/testing';
import { provideRouter } from '@angular/router';

import type { FillView } from '../../api/models';
import { provideApi } from '../../api/provide-api';
import { nextRequest, tick } from '../../../testing/http';
import { FillsPage } from './fills.page';
import { pageThrough } from './server-paging.testing';

function fill(i: number): FillView {
  return {
    id: i,
    order_client_id: `2026-09-25:momentum-v3:T${i}.US:buy`,
    ticker: `T${i}.US`,
    quantity: 10,
    price: 101.5,
    fee: 0.25,
    tick_id: 't1',
    filled_at: '2026-09-25T20:46:00Z',
  };
}

describe('FillsPage', () => {
  let controller: HttpTestingController;

  beforeEach(() => {
    TestBed.configureTestingModule({
      providers: [...provideApi(), provideHttpClientTesting(), provideRouter([])],
    });
    controller = TestBed.inject(HttpTestingController);
  });

  it('Next twice loads offset 50 then 100 and the range reads 101–150', async () => {
    const run = await pageThrough(FillsPage, '/api/orders/fills', 50, fill);
    expect(run.offsets).toEqual([0, 50, 100]);
    expect(run.range).toBe('101–150 of 250');
    expect(run.keptRowsWhileLoading).toEqual([true, true]);
  });

  it('filters by the tick in the URL and links to the order and the run, not raw ids', async () => {
    const fixture = TestBed.createComponent(FillsPage);
    fixture.componentRef.setInput('tick', 't1');
    fixture.detectChanges();
    const req = await nextRequest(controller, '/api/orders/fills');
    expect(req.request.urlWithParams).toContain('tick_id=t1');
    req.flush({ items: [fill(0)], total: 1, limit: 50, offset: 0 });
    for (let i = 0; i < 3; i++) {
      await tick();
      fixture.detectChanges();
    }
    const el = fixture.nativeElement as HTMLElement;
    expect(el.querySelector('label[for="ff-order"]')?.textContent?.trim()).toBe('Order');
    expect(el.textContent).not.toContain('client id');
    const orderLink = el.querySelector(`a[href="/trades/orders/${fill(0).order_client_id}"]`);
    expect(orderLink?.textContent?.trim()).toBe('View order');
    expect(el.querySelector('a[href="/orders/ticks/t1"]')?.textContent?.trim()).toBe('View run');
    expect(el.textContent).not.toContain('2026-09-25:momentum-v3');
    const price = [...el.querySelectorAll('td')].find((td) => td.dataset['label'] === 'Price');
    expect(price?.classList).toContain('num');
  });

  it('explains when fills appear, without jargon', async () => {
    const fixture = TestBed.createComponent(FillsPage);
    fixture.detectChanges();
    (await nextRequest(controller, '/api/orders/fills')).flush({
      items: [],
      total: 0,
      limit: 50,
      offset: 0,
    });
    for (let i = 0; i < 3; i++) {
      await tick();
      fixture.detectChanges();
    }
    const text = (fixture.nativeElement as HTMLElement).textContent ?? '';
    expect(text).toContain('real trading run');
    expect(text).not.toContain(';');
  });
});

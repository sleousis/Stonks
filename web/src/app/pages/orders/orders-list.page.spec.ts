import { HttpTestingController, provideHttpClientTesting } from '@angular/common/http/testing';
import { TestBed } from '@angular/core/testing';
import { Router, provideRouter } from '@angular/router';

import type { OrderView } from '../../api/models';
import { provideApi } from '../../api/provide-api';
import { nextRequest, tick } from '../../../testing/http';
import { OrdersListPage } from './orders-list.page';
import { pageThrough } from './server-paging.testing';

function order(i: number): OrderView {
  return {
    client_id: `2026-09-25:momentum-v3:T${i}.US:buy`,
    ticker: `T${i}.US`,
    side: i % 2 ? 'sell' : 'buy',
    quantity: 10 + i,
    status: 'filled',
    order_type: 'market',
    limit_price: null,
    broker_order_id: null,
    strategy_id: 'momentum-v3',
    tick_id: 't1',
    created_at: '2026-09-25T20:45:00Z',
    updated_at: '2026-09-25T20:45:00Z',
  };
}

describe('OrdersListPage', () => {
  let controller: HttpTestingController;

  beforeEach(() => {
    TestBed.configureTestingModule({
      providers: [...provideApi(), provideHttpClientTesting(), provideRouter([])],
    });
    controller = TestBed.inject(HttpTestingController);
  });

  async function render(items: OrderView[], total = items.length) {
    const fixture = TestBed.createComponent(OrdersListPage);
    fixture.detectChanges();
    (await nextRequest(controller, '/api/orders')).flush({
      items,
      total,
      limit: 50,
      offset: 0,
    });
    for (let i = 0; i < 3; i++) {
      await tick();
      fixture.detectChanges();
    }
    return { fixture, el: fixture.nativeElement as HTMLElement };
  }

  it('Next twice loads offset 50 then 100 and the range reads 101–150', async () => {
    const run = await pageThrough(OrdersListPage, '/api/orders', 50, order);
    expect(run.offsets).toEqual([0, 50, 100]);
    expect(run.range).toBe('101–150 of 250');
    // The previous page stays on screen while the next one loads.
    expect(run.keptRowsWhileLoading).toEqual([true, true]);
  });

  it('links each order to its detail and shows sides as tags, with no client id column', async () => {
    const { el } = await render([order(0), order(1)]);
    const headers = [...el.querySelectorAll('th')].map((th) => th.textContent?.trim());
    expect(headers.join(' ')).not.toContain('Client id');
    const link = el.querySelector<HTMLAnchorElement>('td.cell-title a')!;
    expect(link.textContent?.trim()).toBe('T0.US');
    expect(link.getAttribute('href')).toBe('/trades/orders/2026-09-25:momentum-v3:T0.US:buy');
    const tags = [...el.querySelectorAll('app-side-tag')].map((t) => t.textContent?.trim());
    expect(tags).toEqual(['BBuy', 'SSell']);
    expect(el.querySelector('td a[href="/orders/ticks/t1"]')?.textContent).toContain('View run');
    // Server mode: headers are not sort buttons.
    expect(el.querySelector('th button.sort')).toBeNull();
  });

  it('says where orders come from, in trader words', async () => {
    const { el } = await render([]);
    const text = el.textContent ?? '';
    expect(text).toContain('No orders yet');
    expect(text).toContain('Trading runs tab');
    expect(text).not.toMatch(/stonks |tick runs/);
    expect(el.querySelector('label[for="f-tick"]')?.textContent).toContain('Trading run');
  });

  it('writes a filter to the URL', async () => {
    const { el } = await render([order(0)]);
    const navigate = vi.spyOn(TestBed.inject(Router), 'navigate').mockResolvedValue(true);
    const input = el.querySelector<HTMLInputElement>('#f-ticker')!;
    input.value = 'aapl.us';
    input.dispatchEvent(new Event('change'));
    expect(navigate).toHaveBeenCalledWith(
      [],
      expect.objectContaining({ queryParams: { ticker: 'AAPL.US' } }),
    );
  });
});

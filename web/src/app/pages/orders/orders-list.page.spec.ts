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
    strategy_name: null,
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

  it('names the strategy by its title or a readable name, never the raw id', async () => {
    const { el } = await render([
      { ...order(1), strategy_id: 'starter_trend', strategy_name: 'Starter: trend following' },
      { ...order(2), strategy_id: 'breakout_1a2b3c4d' },
    ]);
    expect(el.textContent).toContain('Starter: trend following');
    expect(el.textContent).toContain('Breakout 1a2b');
    expect(el.textContent).not.toContain('starter_trend');
    expect(el.textContent).not.toContain('breakout_1a2b3c4d');
  });

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

  it('keeps the time on the phone card and hides the price instead (UX-57)', async () => {
    const { el } = await render([order(0)]);
    const cell = (label: string) => el.querySelector(`td[data-label="${label}"]`)!;
    expect(cell('Created').classList).not.toContain('hide-phone');
    expect(cell('Limit or stop').classList).toContain('hide-phone');
    // A market order has no limit or stop: a dash, not the word Market (PO-10).
    expect(cell('Limit or stop').textContent?.trim()).toBe('–');
  });

  it('says what a protective stop does, in plain words', async () => {
    const stop: OrderView = {
      ...order(1),
      client_id: '2026-09-25:momentum-v3:T1.US:buy:stop',
      side: 'sell',
      order_type: 'stop',
      stop_price: 90.5,
      time_in_force: 'gtc',
      protective: true,
      status: 'pending',
      state: 'accepted',
    };
    const { el } = await render([stop, order(0)]);
    const tags = [...el.querySelectorAll('.stop-tag')].map((t) => t.textContent ?? '');
    expect(tags.length).toBe(1);
    expect(tags[0]).toContain('Protective stop:');
    expect(tags[0]).toContain('Sells if the price falls to');
    expect(tags[0]).toContain('90.50');
    expect(tags[0]).toContain('until the position closes');
    const price = el.querySelector('td[data-label="Limit or stop"]')!;
    expect(price.textContent).toContain('Stop');
    expect(el.textContent).toContain('Working');
  });

  it('says where orders come from, in trader words', async () => {
    const { el } = await render([]);
    const text = el.textContent ?? '';
    expect(text).toContain('No orders yet');
    expect(text).toContain('or when you place one by hand');
    expect(text).not.toMatch(/stonks |tick runs/);
    expect(el.querySelector('a[href="/orders/ticks"]')?.textContent).toContain('See trading runs');
    // No box to type a run id in (PO-09).
    expect(el.querySelector('#f-tick')).toBeNull();
  });

  it('says By hand for a manual order and names the caption plainly (PO-10)', async () => {
    const { el } = await render([
      { ...order(0), origin: 'manual', strategy_id: null, tick_id: null },
      order(1),
    ]);
    expect(el.querySelector('caption')?.textContent).toContain(
      'Orders from trading runs and by hand',
    );
    const strategies = [...el.querySelectorAll('td[data-label="Strategy"]')].map((td) =>
      td.textContent?.trim(),
    );
    expect(strategies).toContain('By hand');
  });

  it('picks a strategy by name from the followed ones (PO-09)', async () => {
    const { fixture, el } = await render([order(0)]);
    (await nextRequest(controller, '/api/subscriptions')).flush({
      items: [{ id: 's1', strategy_id: 'breakout_1a2b3c4d', portfolio_id: null }],
      total: 1,
      limit: 500,
      offset: 0,
    });
    for (let i = 0; i < 3; i++) {
      await tick();
      fixture.detectChanges();
    }
    const select = el.querySelector<HTMLSelectElement>('select#f-strategy')!;
    const labels = [...select.options].map((o) => o.textContent?.trim());
    expect(labels).toEqual(['Any strategy', 'Breakout 1a2b']);
  });

  it('shows a run from a link as a chip with a clear button', async () => {
    const fixture = TestBed.createComponent(OrdersListPage);
    fixture.componentRef.setInput('tick', 't1');
    fixture.detectChanges();
    (await nextRequest(controller, '/api/orders')).flush({
      items: [],
      total: 0,
      limit: 50,
      offset: 0,
    });
    for (let i = 0; i < 3; i++) {
      await tick();
      fixture.detectChanges();
    }
    const el = fixture.nativeElement as HTMLElement;
    const navigate = vi.spyOn(TestBed.inject(Router), 'navigate').mockResolvedValue(true);
    expect(el.querySelector('.chip')?.textContent).toContain('One trading run');
    el.querySelector<HTMLButtonElement>('.chip-clear')!.click();
    expect(navigate).toHaveBeenCalledWith(
      [],
      expect.objectContaining({ queryParams: { tick: null } }),
    );
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

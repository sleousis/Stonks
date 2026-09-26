import { HttpTestingController, provideHttpClientTesting } from '@angular/common/http/testing';
import { type ComponentFixture, TestBed } from '@angular/core/testing';
import { provideRouter } from '@angular/router';

import type { OrderView, TickRunWithOrders } from '../../api/models';
import { provideApi } from '../../api/provide-api';
import { nextRequest, tick } from '../../../testing/http';
import { TickDetailPage } from './tick-detail.page';

const ORDER: OrderView = {
  client_id: '2026-09-25:momentum-v3:AAPL.US:buy',
  broker_order_id: null,
  created_at: '2026-09-25T21:00:01Z',
  updated_at: '2026-09-25T21:00:02Z',
  limit_price: null,
  order_type: 'market',
  quantity: 10,
  side: 'buy',
  status: 'rejected',
  strategy_id: 'momentum-v3',
  tick_id: 't1',
  ticker: 'AAPL.US',
};

const TICK: TickRunWithOrders = {
  id: 't1',
  started_at: '2026-09-25T21:00:00Z',
  finished_at: '2026-09-25T21:00:04Z',
  status: 'partial',
  orders: [{ ...ORDER, status_reason: 'insufficient buying power' }],
  summary: {
    winner_strategy_id: 'momentum-v3',
    winner_expected_return: 0.012,
    orders_placed: 1,
    fills: 0,
    risk_adjustments: [
      {
        ticker: 'AAPL.US',
        side: 'buy',
        rule: 'max_position_weight',
        original_quantity: 40,
        adjusted_quantity: 10,
        reason: 'position would exceed 10% of value',
      },
    ],
    shadow: [
      {
        strategy_id: 'value-v1',
        status: 'evaluated',
        decisions: 3,
        fills: 2,
        total_value: 101_000,
      },
      { strategy_id: 'broken-v0', status: 'failed', error: 'KeyError: close' },
    ],
  },
};

describe('TickDetailPage', () => {
  let fixture: ComponentFixture<TickDetailPage>;
  let controller: HttpTestingController;
  let el: HTMLElement;

  beforeEach(() => {
    TestBed.configureTestingModule({
      imports: [TickDetailPage],
      providers: [...provideApi(), provideHttpClientTesting(), provideRouter([])],
    });
    controller = TestBed.inject(HttpTestingController);
    fixture = TestBed.createComponent(TickDetailPage);
    fixture.componentRef.setInput('id', 't1');
    el = fixture.nativeElement;
    fixture.detectChanges();
  });

  afterEach(() => controller.verify());

  async function settle(): Promise<void> {
    for (let i = 0; i < 4; i++) {
      await tick(5);
      fixture.detectChanges();
    }
  }

  it('shows the decision, risk adjustments, orders with reasons and shadow outcomes', async () => {
    (await nextRequest(controller, '/api/ticks/t1')).flush(TICK);
    const fills = await nextRequest(controller, '/api/orders/fills');
    expect(fills.request.urlWithParams).toContain('tick_id=t1');
    fills.flush({ items: [], total: 0, limit: 200, offset: 0 });
    await settle();

    const text = el.textContent ?? '';
    expect(text).toContain('momentum-v3');
    expect(text).toContain('+1.20%');

    const risk = el.querySelector('section[aria-labelledby="risk-title"]');
    expect(risk?.textContent).toContain('Max position weight');
    expect(risk?.textContent).toContain('position would exceed 10% of value');

    const orders = el.querySelector('section[aria-labelledby="tick-orders-title"]');
    expect(orders?.querySelector('app-status-pill')?.getAttribute('data-tone')).toBe('negative');
    expect(orders?.textContent).toContain('insufficient buying power');

    const shadow = el.querySelector('section[aria-labelledby="shadow-title"]');
    expect(shadow?.textContent).toContain('value-v1');
    expect(shadow?.textContent).toContain('KeyError: close');
  });

  it('names the run by its time and keeps the id as secondary text', async () => {
    (await nextRequest(controller, '/api/ticks/t1')).flush(TICK);
    (await nextRequest(controller, '/api/orders/fills')).flush({
      items: [],
      total: 0,
      limit: 200,
      offset: 0,
    });
    await settle();
    const h2 = el.querySelector('.detail-head h2')?.textContent ?? '';
    expect(h2).toContain('Trading run of');
    expect(h2).not.toContain('t1');
    expect(el.querySelector('.run-id')?.textContent).toContain('t1');
    const risk = el.querySelector('section[aria-labelledby="risk-title"]');
    expect(risk?.querySelector('app-side-tag')?.textContent).toContain('Buy');
    expect(el.querySelector('.back-link')?.textContent).toContain('All trading runs');
  });

  it('shows a link when total > items', async () => {
    (await nextRequest(controller, '/api/ticks/t1')).flush(TICK);
    const items = Array.from({ length: 200 }, (_, i) => ({
      id: i,
      order_client_id: `o${i}`,
      ticker: 'AAPL.US',
      quantity: 1,
      price: 100,
      fee: 0,
      tick_id: 't1',
      filled_at: '2026-09-25T21:00:03Z',
    }));
    (await nextRequest(controller, '/api/orders/fills')).flush({
      items,
      total: 340,
      limit: 200,
      offset: 0,
    });
    await settle();
    const fills = el.querySelector('section[aria-labelledby="tick-fills-title"]')!;
    const link = fills.querySelector<HTMLAnchorElement>('.panel-head a')!;
    expect(link.textContent).toContain('200 of 340');
    expect(link.getAttribute('href')).toBe('/orders/fills?tick=t1');
    expect(fills.querySelector('td a[href="/trades/orders/o0"]')?.textContent).toContain(
      'View order',
    );
  });

  it('shows no cap link when every fill fits', async () => {
    (await nextRequest(controller, '/api/ticks/t1')).flush(TICK);
    (await nextRequest(controller, '/api/orders/fills')).flush({
      items: [],
      total: 0,
      limit: 200,
      offset: 0,
    });
    await settle();
    const fills = el.querySelector('section[aria-labelledby="tick-fills-title"]')!;
    expect(fills.querySelector('.panel-head a')).toBeNull();
  });

  it('names the exit strategy when no candidate qualified', async () => {
    (await nextRequest(controller, '/api/ticks/t1')).flush({
      ...TICK,
      orders: [],
      summary: { winner_strategy_id: null, reason: 'no_candidates', exit_strategy_id: 'value-v1' },
    });
    (await nextRequest(controller, '/api/orders/fills')).flush({
      items: [],
      total: 0,
      limit: 200,
      offset: 0,
    });
    await settle();

    expect(el.textContent).toContain('Exit strategy');
    expect(el.textContent).toContain('Exited positions of');
    expect(el.textContent).toContain('No candidates');
  });
});

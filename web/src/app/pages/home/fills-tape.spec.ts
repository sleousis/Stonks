import { HttpTestingController, provideHttpClientTesting } from '@angular/common/http/testing';
import { TestBed } from '@angular/core/testing';
import { provideRouter } from '@angular/router';

import type { FillView, OrderView } from '../../api/models';
import { provideApi } from '../../api/provide-api';
import { nextRequest, page, tick } from '../../../testing/http';
import { book } from '../../../testing/portfolio-fixtures';
import { FillsTape, TAPE_MOVES_FROM, latestFills, sideByOrder } from './fills-tape';

function fill(overrides: Partial<FillView>): FillView {
  return {
    id: 1,
    order_client_id: 'c1',
    tick_id: 't1',
    ticker: 'AAA.US',
    quantity: 12,
    price: 101.25,
    fee: 0,
    filled_at: new Date(Date.now() - 60_000).toISOString(),
    ...overrides,
  };
}

function order(clientId: string, side: string): OrderView {
  return { client_id: clientId, side } as OrderView;
}

describe('fills tape helpers', () => {
  it('keeps the latest session with fills, oldest first', () => {
    const now = new Date('2026-09-26T12:00:00Z');
    const fills = [
      fill({ id: 1, filled_at: '2026-09-26T11:00:00Z' }),
      fill({ id: 2, filled_at: '2026-09-26T09:00:00Z' }),
      fill({ id: 3, filled_at: '2026-09-24T11:00:00Z' }),
    ];
    const latest = latestFills(fills, now);
    expect(latest.fills.map((f) => f.id)).toEqual([2, 1]);
    expect(latest.today).toBe(true);
  });

  it('shows the last session on a quiet day, and nothing older than a week', () => {
    const monday = new Date('2026-09-28T09:00:00Z');
    const friday = [fill({ id: 5, filled_at: '2026-09-25T20:00:00Z' })];
    expect(latestFills(friday, monday)).toMatchObject({ today: false });
    expect(latestFills(friday, monday).fills).toHaveLength(1);
    expect(latestFills(friday, new Date('2026-10-09T09:00:00Z')).fills).toHaveLength(0);
  });

  it('reads the side from the order', () => {
    expect(sideByOrder([order('a', 'buy'), order('b', 'sell')]).get('b')).toBe('sell');
  });
});

describe('FillsTape', () => {
  let controller: HttpTestingController;

  beforeEach(() => {
    TestBed.configureTestingModule({
      providers: [provideRouter([]), ...provideApi(), provideHttpClientTesting()],
    });
    controller = TestBed.inject(HttpTestingController);
  });

  afterEach(() => controller.verify());

  async function render(fills: FillView[], orders: OrderView[]) {
    const fixture = TestBed.createComponent(FillsTape);
    fixture.detectChanges();
    (await nextRequest(controller, '/api/portfolios')).flush(
      page([book({ id: 'pf_1', name: 'Main' })]),
    );
    (await nextRequest(controller, '/api/orders/fills')).flush(page(fills));
    (await nextRequest(controller, '/api/orders')).flush(page(orders));
    await tick();
    fixture.detectChanges();
    return fixture.nativeElement as HTMLElement;
  }

  it('prints each fill with its side, quantity at price, and sits still when short', async () => {
    const el = await render(
      [
        fill({ id: 1, order_client_id: 'c1' }),
        fill({ id: 2, order_client_id: 'c2', ticker: 'BBB.US' }),
      ],
      [order('c1', 'buy'), order('c2', 'sell')],
    );
    const items = [...el.querySelectorAll('.run:not([aria-hidden]) li')];
    expect(items).toHaveLength(2);
    expect(items[0].querySelector('app-side-tag .tag')?.getAttribute('data-side')).toBe('buy');
    expect(items[1].querySelector('app-side-tag .tag')?.getAttribute('data-side')).toBe('sell');
    expect(items[0].textContent).toContain('AAA.US');
    expect(items[0].textContent).toContain('$101.25');
    expect(el.querySelector('.track.moving')).toBeNull();
    expect(el.querySelector('a.all')?.getAttribute('href')).toBe('/orders/fills');
  });

  it('moves once it is full, with a hidden copy for the loop', async () => {
    const fills = Array.from({ length: TAPE_MOVES_FROM }, (_, i) =>
      fill({ id: i + 1, order_client_id: `c${i}` }),
    );
    const el = await render(fills, []);
    expect(el.querySelector('.track.moving')).not.toBeNull();
    expect(el.querySelectorAll('.run[aria-hidden="true"] li')).toHaveLength(TAPE_MOVES_FROM);
  });

  it('says so when nothing filled this week', async () => {
    const el = await render([fill({ filled_at: '2020-01-01T00:00:00Z' })], []);
    expect(el.textContent).toContain('No fills this week');
  });
});

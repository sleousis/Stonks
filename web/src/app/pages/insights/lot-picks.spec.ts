import {
  HttpTestingController,
  type TestRequest,
  provideHttpClientTesting,
} from '@angular/common/http/testing';
import { type ComponentFixture, TestBed } from '@angular/core/testing';

import type { FillView } from '../../api/models';
import { provideApi } from '../../api/provide-api';
import { PortfolioContextService } from '../../core/portfolio/portfolio-context.service';
import { nextRequest, page, tick } from '../../../testing/http';
import { LotPicks, candidateLots, picksError } from './lot-picks';

const query = (req: TestRequest) => new URL(req.request.urlWithParams, 'http://x').searchParams;

function fill(over: Partial<FillView> & Pick<FillView, 'id'>): FillView {
  return {
    ticker: 'AAA.US',
    side: 'buy',
    quantity: 10,
    price: 100,
    fee: 1,
    filled_at: '2026-09-01T15:00:00Z',
    order_client_id: `o${over.id}`,
    tick_id: null,
    ...over,
  };
}

const BUY_OLD = fill({ id: 1, filled_at: '2026-08-01T15:00:00Z', price: 90 });
const BUY_NEW = fill({ id: 2, filled_at: '2026-08-20T15:00:00Z', price: 110 });
const OTHER = fill({ id: 3, ticker: 'BBB.US' });
const SALE = fill({ id: 4, side: 'sell', quantity: 12, filled_at: '2026-09-10T15:00:00Z' });
const LATER_BUY = fill({ id: 5, filled_at: '2026-09-20T15:00:00Z' });
const FILLS = [BUY_OLD, BUY_NEW, OTHER, SALE, LATER_BUY];

describe('lot picks', () => {
  it('offers only earlier buys of the same ticker, oldest first', () => {
    expect(candidateLots(FILLS, SALE).map((f) => f.id)).toEqual([1, 2]);
  });

  it('refuses picks above a lot or the sale', () => {
    const lots = [BUY_OLD, BUY_NEW];
    expect(picksError(SALE, lots, { 1: '10', 2: '2' })).toBeNull();
    expect(picksError(SALE, lots, { 1: '11' })).toContain('only 10 shares');
    expect(picksError(SALE, lots, { 1: '10', 2: '5' })).toContain('add up to 15');
    expect(picksError(SALE, lots, { 1: '-1' })).toContain('0 or more');
  });
});

describe('LotPicks', () => {
  let fixture: ComponentFixture<LotPicks>;
  let http: HttpTestingController;
  let el: HTMLElement;

  beforeEach(() => {
    TestBed.configureTestingModule({
      providers: [
        ...provideApi(),
        provideHttpClientTesting(),
        {
          provide: PortfolioContextService,
          useValue: { query: () => ({ portfolio_id: 'pf_1' }) },
        },
      ],
    });
    http = TestBed.inject(HttpTestingController);
  });

  afterEach(() => http.verify());

  async function render(fills = FILLS): Promise<void> {
    fixture = TestBed.createComponent(LotPicks);
    fixture.componentRef.setInput('portfolioId', 'pf_1');
    fixture.componentRef.setInput('canManage', true);
    el = fixture.nativeElement;
    fixture.detectChanges();
    (await nextRequest(http, '/api/orders/fills')).flush(page(fills));
    await tick(5);
    fixture.detectChanges();
  }

  async function chooseSale(): Promise<void> {
    const select = el.querySelector<HTMLSelectElement>('#lot-sale')!;
    select.value = String(SALE.id);
    select.dispatchEvent(new Event('change'));
    fixture.detectChanges();
    const req = await nextRequest(http, '/api/tax/lots/picks');
    expect(query(req).get('sell_fill_id')).toBe(String(SALE.id));
    req.flush(page([{ sell_fill_id: 4, buy_fill_id: 2, quantity: 5, ticker: 'AAA.US' }]));
    await tick(5);
    fixture.detectChanges();
  }

  it('says there is nothing to pick without sales', async () => {
    await render([BUY_OLD]);
    expect(el.textContent).toContain('No sales yet');
  });

  it('loads the saved picks of a sale and saves new ones', async () => {
    await render();
    expect(el.querySelectorAll('#lot-sale option')).toHaveLength(2);
    await chooseSale();
    const inputs = el.querySelectorAll<HTMLInputElement>('li input');
    expect(inputs).toHaveLength(2);
    expect(inputs[1].value).toBe('5');
    expect(el.textContent).toContain('Picked 5 of 12. The other 7 close oldest first.');

    inputs[0].value = '7';
    inputs[0].dispatchEvent(new Event('input'));
    fixture.detectChanges();
    expect(el.textContent).toContain('Picked all 12 shares.');
    el.querySelector<HTMLFormElement>('form')!.dispatchEvent(new Event('submit'));
    const put = await nextRequest(http, '/api/tax/lots/picks', 'PUT');
    expect(put.request.body).toEqual({
      sell_fill_id: 4,
      picks: [
        { buy_fill_id: 1, quantity: 7 },
        { buy_fill_id: 2, quantity: 5 },
      ],
    });
    put.flush(page([]));
    (await nextRequest(http, '/api/tax/lots/picks')).flush(page([]));
    await tick(5);
  });

  it('blocks saving picks that add up to more than the sale', async () => {
    await render();
    await chooseSale();
    const input = el.querySelectorAll<HTMLInputElement>('li input')[0];
    input.value = '10';
    input.dispatchEvent(new Event('input'));
    fixture.detectChanges();
    expect(el.querySelector('.total.error')?.textContent).toContain('add up to 15');
    const save = [...el.querySelectorAll('button')].find((b) =>
      b.textContent?.includes('Save picks'),
    )!;
    expect(save.disabled).toBe(true);
  });

  it('goes back to oldest first with an empty list', async () => {
    await render();
    await chooseSale();
    [...el.querySelectorAll('button')]
      .find((b) => b.textContent?.includes('oldest first'))!
      .click();
    const put = await nextRequest(http, '/api/tax/lots/picks', 'PUT');
    expect(put.request.body).toEqual({ sell_fill_id: 4, picks: [] });
    put.flush(page([]));
    (await nextRequest(http, '/api/tax/lots/picks')).flush(page([]));
    await tick(5);
  });
});

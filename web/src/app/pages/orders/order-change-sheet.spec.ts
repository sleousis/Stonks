import { HttpTestingController, provideHttpClientTesting } from '@angular/common/http/testing';
import { type ComponentFixture, TestBed } from '@angular/core/testing';

import type { OrderView } from '../../api/models';
import { provideApi } from '../../api/provide-api';
import { PortfolioContextService } from '../../core/portfolio/portfolio-context.service';
import { nextRequest, tick } from '../../../testing/http';
import { OrderChangeSheet } from './order-change-sheet';

const ORDER: OrderView = {
  client_id: 'mk1',
  ticker: 'AAA.US',
  side: 'sell',
  quantity: 10,
  status: 'pending',
  order_type: 'market',
  limit_price: null,
  broker_order_id: null,
  strategy_id: null,
  tick_id: null,
  created_at: '2026-09-25T20:45:00Z',
  updated_at: '2026-09-25T20:45:00Z',
};

describe('OrderChangeSheet', () => {
  let fixture: ComponentFixture<OrderChangeSheet>;
  let http: HttpTestingController;

  beforeEach(() => {
    TestBed.configureTestingModule({
      providers: [
        ...provideApi(),
        provideHttpClientTesting(),
        { provide: PortfolioContextService, useValue: { query: () => ({ portfolio_id: 'pf_1' }) } },
      ],
    });
    http = TestBed.inject(HttpTestingController);
    fixture = TestBed.createComponent(OrderChangeSheet);
    fixture.detectChanges();
  });

  afterEach(() => http.verify());

  const form = () => (fixture.nativeElement as HTMLElement).querySelector<HTMLFormElement>('form')!;

  it('asks for a reason before it sends anything, and keeps the order on cancel', async () => {
    const result = fixture.componentInstance.open(ORDER, false);
    fixture.detectChanges();
    expect(form().querySelector('#co-limit')).toBeNull();
    form().querySelector<HTMLButtonElement>('button[type="submit"]')!.click();
    fixture.detectChanges();
    expect(form().querySelector('#co-reason-hint')?.textContent).toContain('Say why');
    await tick(5);
    expect(http.match(() => true)).toHaveLength(0);
    [...form().querySelectorAll('button')].find((b) => b.textContent?.includes('Keep'))!.click();
    expect(await result).toBeNull();
  });

  it('cannot be closed while the change is on its way, and resolves with the new order', async () => {
    const result = fixture.componentInstance.open(ORDER, false);
    fixture.detectChanges();
    const reason = form().querySelector<HTMLTextAreaElement>('#co-reason')!;
    reason.value = 'smaller';
    reason.dispatchEvent(new Event('input'));
    fixture.detectChanges();
    form().querySelector<HTMLButtonElement>('button[type="submit"]')!.click();
    const req = await nextRequest(http, '/api/orders/mk1/change', 'POST');
    fixture.detectChanges();
    // Keep the order and Escape do nothing now: the server is already changing it.
    const keep = [...form().querySelectorAll('button')].find((b) =>
      b.textContent?.includes('Keep'),
    )!;
    expect(keep.disabled).toBe(true);
    (fixture.componentInstance as unknown as { close(r: null): void }).close(null);
    const placed = { order: { ...ORDER, client_id: 'mk2', quantity: 8 } };
    req.flush(placed);
    expect(await result).toEqual(placed);
  });

  it('shows a failure that is not a refusal', async () => {
    void fixture.componentInstance.open(ORDER, false);
    fixture.detectChanges();
    const reason = form().querySelector<HTMLTextAreaElement>('#co-reason')!;
    reason.value = 'smaller';
    reason.dispatchEvent(new Event('input'));
    fixture.detectChanges();
    form().querySelector<HTMLButtonElement>('button[type="submit"]')!.click();
    const req = await nextRequest(http, '/api/orders/mk1/change', 'POST');
    expect(req.request.body).toMatchObject({ quantity: 10, limit_price: null, reason: 'smaller' });
    req.flush(
      {
        title: 'Conflict',
        status: 409,
        code: 'conflict',
        detail: 'only a working order can change',
      },
      { status: 409, statusText: 'Conflict' },
    );
    await tick();
    fixture.detectChanges();
    expect(form().querySelector('.failure')?.textContent).toContain('only a working order');
  });
});

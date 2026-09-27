import type { Mock } from 'vitest';
import { HttpTestingController, provideHttpClientTesting } from '@angular/common/http/testing';
import { computed, signal } from '@angular/core';
import { type ComponentFixture, TestBed } from '@angular/core/testing';
import { provideRouter } from '@angular/router';

import type { OrderView } from '../../api/models';
import { provideApi } from '../../api/provide-api';
import { SessionService } from '../../core/auth/session.service';
import { StepUpService } from '../../core/auth/step-up.service';
import { ToastService } from '../../core/notify/toast.service';
import { PortfolioContextService } from '../../core/portfolio/portfolio-context.service';
import { nextRequest, tick } from '../../../testing/http';
import { book } from '../../../testing/portfolio-fixtures';
import { answerDialog } from '../../../testing/status-dialog';
import { ManualOrdersList, isWorking } from './manual-orders-list';

function order(over: Partial<OrderView> = {}): OrderView {
  return {
    client_id: 'mk1',
    ticker: 'AAA.US',
    side: 'buy',
    quantity: 10,
    status: 'pending',
    order_type: 'limit',
    limit_price: 24,
    broker_order_id: null,
    strategy_id: null,
    tick_id: null,
    origin: 'manual',
    created_at: '2026-09-25T20:45:00Z',
    updated_at: '2026-09-25T20:45:00Z',
    ...over,
  };
}

const pageOf = (items: OrderView[]) => ({ items, total: items.length, limit: 50, offset: 0 });

describe('ManualOrdersList', () => {
  let fixture: ComponentFixture<ManualOrdersList>;
  let http: HttpTestingController;
  let ensure: Mock<(reason?: string) => Promise<boolean>>;
  const live = signal(false);

  beforeEach(() => {
    live.set(false);
    ensure = vi.fn(async () => true);
    const current = signal(book({ id: 'pf_1', name: 'Tess book' }));
    TestBed.configureTestingModule({
      providers: [
        provideRouter([]),
        ...provideApi(),
        provideHttpClientTesting(),
        {
          provide: PortfolioContextService,
          useValue: {
            current,
            live: computed(() => live()),
            selectedId: computed(() => 'pf_1'),
            query: () => ({ portfolio_id: 'pf_1' }),
          },
        },
        { provide: StepUpService, useValue: { ensure } },
      ],
    });
    http = TestBed.inject(HttpTestingController);
    const session = TestBed.inject(SessionService);
    vi.spyOn(session, 'can').mockReturnValue(true);
    vi.spyOn(session, 'whyNot').mockReturnValue(null);
  });

  afterEach(() => http.verify());

  async function settle() {
    for (let i = 0; i < 3; i++) {
      await tick();
      fixture.detectChanges();
    }
  }

  async function render(items: OrderView[]) {
    fixture = TestBed.createComponent(ManualOrdersList);
    fixture.detectChanges();
    const req = await nextRequest(http, '/api/orders');
    expect(req.request.urlWithParams).toContain('origin=manual');
    expect(req.request.urlWithParams).toContain('portfolio_id=pf_1');
    req.flush(pageOf(items));
    await settle();
    return fixture.nativeElement as HTMLElement;
  }

  const byText = (el: HTMLElement, text: string) =>
    [...el.querySelectorAll<HTMLButtonElement>('button')].find((b) =>
      b.textContent?.trim().startsWith(text),
    );

  it('offers change and cancel only on working orders', async () => {
    const el = await render([order(), order({ client_id: 'k2', status: 'filled' })]);
    expect(el.querySelectorAll('button').length).toBeGreaterThanOrEqual(2);
    expect(el.textContent).toContain('None');
    expect(isWorking({ status: 'partially_filled' })).toBe(true);
    expect(isWorking({ status: 'cancelled' })).toBe(false);
  });

  it('says what fills an empty list', async () => {
    const el = await render([]);
    expect(el.textContent).toContain('No orders by hand yet');
  });

  it('cancels a working order with a reason', async () => {
    const toast = vi.spyOn(TestBed.inject(ToastService), 'success');
    const el = await render([order()]);
    byText(el, 'Cancel')!.click();
    await settle();
    answerDialog(fixture, { reason: 'changed my mind' });
    const req = await nextRequest(http, '/api/orders/mk1/cancel', 'POST');
    expect(req.request.body).toEqual({ portfolio_id: 'pf_1', reason: 'changed my mind' });
    req.flush({ client_id: 'mk1', cancelled: true, status: 'cancelled' });
    (await nextRequest(http, '/api/orders')).flush(pageOf([order({ status: 'cancelled' })]));
    await settle();
    expect(toast).toHaveBeenCalledWith('Cancelled the order for AAA.US.');
  });

  it('changes a working order and shows a refusal inside the sheet', async () => {
    live.set(true);
    const el = await render([order()]);
    byText(el, 'Change')!.click();
    await settle();
    expect(ensure).toHaveBeenCalled();
    const form = el.querySelector<HTMLFormElement>('app-order-change-sheet form')!;
    expect(form.textContent).toContain('LIVE');
    const qty = form.querySelector<HTMLInputElement>('#co-qty')!;
    expect(qty.value).toBe('10');
    qty.value = '50';
    qty.dispatchEvent(new Event('input'));
    const reason = form.querySelector<HTMLTextAreaElement>('#co-reason')!;
    reason.value = 'bigger';
    reason.dispatchEvent(new Event('input'));
    fixture.detectChanges();
    form.querySelector<HTMLButtonElement>('button[type="submit"]')!.click();
    const refused = await nextRequest(http, '/api/orders/mk1/change', 'POST');
    expect(refused.request.body).toMatchObject({
      quantity: 50,
      limit_price: 24,
      reason: 'bigger',
      allow_reduce: false,
    });
    refused.flush(
      {
        title: 'Conflict',
        status: 409,
        code: 'order_refused',
        detail: 'the risk rules allow 20 of the 50 asked for',
        risk_adjustments: [
          {
            rule: 'max_position_weight',
            ticker: 'AAA.US',
            side: 'buy',
            original_quantity: 50,
            adjusted_quantity: 20,
            reason: 'cap',
          },
        ],
      },
      { status: 409, statusText: 'Conflict' },
    );
    await settle();
    const box = form.querySelector<HTMLInputElement>('app-order-refusal input[type="checkbox"]')!;
    box.click();
    fixture.detectChanges();
    form.querySelector<HTMLButtonElement>('button[type="submit"]')!.click();
    const again = await nextRequest(http, '/api/orders/mk1/change', 'POST');
    expect(again.request.body.allow_reduce).toBe(true);
    again.flush({
      client_id: 'mk1.r1',
      portfolio_id: 'pf_1',
      ticker: 'AAA.US',
      side: 'buy',
      quantity: 20,
      requested_quantity: 50,
      order_type: 'limit',
      limit_price: 24,
      reference_price: 25,
      status: 'pending',
      live: true,
    });
    (await nextRequest(http, '/api/orders')).flush(pageOf([order({ quantity: 20 })]));
    await settle();
    expect(el.querySelector('app-order-change-sheet form')).toBeNull();
  });
});

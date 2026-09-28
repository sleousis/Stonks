import type { Mock } from 'vitest';
import { HttpTestingController, provideHttpClientTesting } from '@angular/common/http/testing';
import { computed, signal } from '@angular/core';
import { type ComponentFixture, TestBed } from '@angular/core/testing';
import { provideRouter } from '@angular/router';

import type { OrderDraftView } from '../../api/models';
import { provideApi } from '../../api/provide-api';
import { SessionService } from '../../core/auth/session.service';
import { StepUpService } from '../../core/auth/step-up.service';
import { type ConfirmOptions, ConfirmService } from '../../core/confirm/confirm.service';
import { ToastService } from '../../core/notify/toast.service';
import { PortfolioContextService } from '../../core/portfolio/portfolio-context.service';
import { nextRequest, page, tick } from '../../../testing/http';
import { book } from '../../../testing/portfolio-fixtures';
import { answerDialog } from '../../../testing/status-dialog';
import { provideFakeTax } from '../../../testing/fake-tax';
import { OrderDraftsPage, draftSource, draftStatus } from './order-drafts.page';

function draft(over: Partial<OrderDraftView> = {}): OrderDraftView {
  return {
    id: 'od_1',
    portfolio_id: 'pf_1',
    ticker: 'AAA.US',
    side: 'buy',
    quantity: 5,
    order_type: 'market',
    limit_price: null,
    reference_price: 25,
    notional: 125,
    reason: 'momentum turned up',
    source: 'assistant',
    status: 'pending',
    client_id: null,
    conversation_id: 'conv_1',
    created_at: '2026-09-27T10:00:00Z',
    expires_at: '2026-09-28T10:00:00Z',
    decided_at: null,
    decided_by: null,
    decision_note: null,
    ...over,
  };
}

describe('OrderDraftsPage', () => {
  let fixture: ComponentFixture<OrderDraftsPage>;
  let http: HttpTestingController;
  let confirm: Mock<(o: ConfirmOptions) => Promise<boolean>>;
  let ensure: Mock<(reason?: string) => Promise<boolean>>;

  function setup(trading: 'paper' | 'live' = 'paper') {
    ensure = vi.fn(async () => true);
    const options = signal([book({ id: 'pf_1', name: 'Tess book', trading })]);
    TestBed.configureTestingModule({
      providers: [
        provideRouter([]),
        ...provideApi(),
        provideHttpClientTesting(),
        provideFakeTax(),
        {
          provide: PortfolioContextService,
          useValue: { options: computed(() => options()), query: () => ({}) },
        },
        { provide: StepUpService, useValue: { ensure } },
      ],
    });
    http = TestBed.inject(HttpTestingController);
    const session = TestBed.inject(SessionService);
    vi.spyOn(session, 'can').mockReturnValue(true);
    vi.spyOn(session, 'whyNot').mockReturnValue(null);
    confirm = vi.fn<(o: ConfirmOptions) => Promise<boolean>>(async () => true);
    vi.spyOn(TestBed.inject(ConfirmService), 'confirm').mockImplementation(confirm);
  }

  afterEach(() => http.verify());

  async function settle() {
    for (let i = 0; i < 3; i++) {
      await tick();
      fixture.detectChanges();
    }
  }

  async function render(items: OrderDraftView[]) {
    fixture = TestBed.createComponent(OrderDraftsPage);
    fixture.detectChanges();
    const req = await nextRequest(http, '/api/orders/drafts');
    expect(req.request.urlWithParams).toContain('status=pending');
    req.flush(page(items));
    await settle();
    return fixture.nativeElement as HTMLElement;
  }

  const button = (el: HTMLElement, text: string) =>
    [...el.querySelectorAll<HTMLButtonElement>('article button')].find((b) =>
      b.textContent?.trim().startsWith(text),
    )!;

  it('shows each waiting draft as a ticket', async () => {
    setup();
    const el = await render([draft()]);
    const ticket = el.querySelector('article.ticket')!;
    expect(ticket.textContent).toContain('Buy 5 AAA.US');
    expect(ticket.textContent).toContain('Tess book');
    expect(ticket.textContent).toContain('$125.00');
    expect(ticket.textContent).toContain('momentum turned up');
    expect(ticket.textContent).toContain('From The assistant');
    expect(ticket.textContent).toContain('PAPER');
  });

  it('says nothing is waiting', async () => {
    setup();
    const el = await render([]);
    expect(el.textContent).toContain('Nothing waiting for you');
  });

  it('approves with a fresh code and the ticket, then reads the list again', async () => {
    setup();
    const toast = vi.spyOn(TestBed.inject(ToastService), 'success');
    const el = await render([draft()]);
    button(el, 'Approve and place').click();
    const req = await nextRequest(http, '/api/orders/drafts/od_1/approve', 'POST');
    expect(ensure).toHaveBeenCalled();
    expect(confirm).toHaveBeenCalledWith(
      expect.objectContaining({
        confirmLabel: 'Approve and place',
        typedConfirmation: undefined,
        ticket: expect.objectContaining({ side: 'buy', live: false }),
      }),
    );
    req.flush({
      draft: draft({ status: 'placed', client_id: 'manual:pf_1:x' }),
      order: {
        client_id: 'manual:pf_1:x',
        portfolio_id: 'pf_1',
        ticker: 'AAA.US',
        side: 'buy',
        quantity: 5,
        requested_quantity: 5,
        order_type: 'market',
        limit_price: null,
        reference_price: 25,
        status: 'filled',
        live: false,
      },
    });
    (await nextRequest(http, '/api/orders/drafts')).flush(page([]));
    await settle();
    expect(toast).toHaveBeenCalledWith('Placed and filled: buy 5 AAA.US.');
  });

  it('types the ticker for a real-money book and shows a refusal on the draft', async () => {
    setup('live');
    const el = await render([draft()]);
    expect(el.querySelector('article.ticket')?.classList).toContain('live');
    button(el, 'Approve and place').click();
    const req = await nextRequest(http, '/api/orders/drafts/od_1/approve', 'POST');
    expect(confirm).toHaveBeenCalledWith(
      expect.objectContaining({ tone: 'danger', typedConfirmation: 'AAA.US' }),
    );
    req.flush(
      {
        title: 'Conflict',
        status: 409,
        code: 'order_refused',
        detail: 'the risk rules refuse this order',
      },
      { status: 409, statusText: 'Conflict' },
    );
    (await nextRequest(http, '/api/orders/drafts')).flush(page([draft()]));
    await settle();
    expect(el.querySelector('.failure')?.textContent).toContain('Not placed');
  });

  it('does nothing without the code', async () => {
    setup();
    const el = await render([draft()]);
    ensure.mockResolvedValueOnce(false);
    button(el, 'Approve and place').click();
    await tick(5);
    expect(confirm).not.toHaveBeenCalled();
    expect(http.match(() => true)).toHaveLength(0);
  });

  it('rejects with an optional note', async () => {
    setup();
    const el = await render([draft()]);
    button(el, 'Reject').click();
    await settle();
    answerDialog(fixture, { reason: 'not now' });
    const req = await nextRequest(http, '/api/orders/drafts/od_1/reject', 'POST');
    expect(req.request.body).toEqual({ note: 'not now' });
    req.flush(draft({ status: 'rejected' }));
    (await nextRequest(http, '/api/orders/drafts')).flush(page([]));
    await settle();
  });

  it('reads another status when the filter changes', async () => {
    setup();
    const el = await render([]);
    [...el.querySelectorAll<HTMLButtonElement>('[role="radio"]')]
      .find((b) => b.textContent?.trim() === 'All')!
      .click();
    fixture.detectChanges();
    const req = await nextRequest(http, '/api/orders/drafts');
    expect(req.request.urlWithParams).not.toContain('status=');
    req.flush(page([draft({ status: 'expired' })]));
    await settle();
    expect(el.textContent).toContain('Expired');
  });
});

describe('draft words', () => {
  it('names statuses and sources', () => {
    expect(draftStatus('pending').label).toBe('Waiting for you');
    expect(draftSource('mcp')).toBe('An agent tool');
  });
});

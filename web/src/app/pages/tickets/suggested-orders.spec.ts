import type { Mock } from 'vitest';
import { HttpTestingController, provideHttpClientTesting } from '@angular/common/http/testing';
import { Component, computed, signal } from '@angular/core';
import { type ComponentFixture, TestBed } from '@angular/core/testing';
import { provideRouter } from '@angular/router';

import type { OrderDraftView } from '../../api/models';
import { provideApi } from '../../api/provide-api';
import { SessionService } from '../../core/auth/session.service';
import { StepUpService } from '../../core/auth/step-up.service';
import { type ConfirmOptions, ConfirmService } from '../../core/confirm/confirm.service';
import { ToastService } from '../../core/notify/toast.service';
import { PortfolioContextService } from '../../core/portfolio/portfolio-context.service';
import { nextRequest, tick } from '../../../testing/http';
import { book } from '../../../testing/portfolio-fixtures';
import { answerDialog, confirmButton, dialogForm } from '../../../testing/status-dialog';
import { provideFakeTax } from '../../../testing/fake-tax';
import { SuggestedOrders, draftSource, draftStatus } from './suggested-orders';

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

@Component({
  imports: [SuggestedOrders],
  template: `<app-suggested-orders [drafts]="drafts()" (changed)="changes = changes + 1" />`,
})
class Host {
  readonly drafts = signal<OrderDraftView[]>([]);
  changes = 0;
}

describe('SuggestedOrders', () => {
  let fixture: ComponentFixture<Host>;
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
    fixture = TestBed.createComponent(Host);
    fixture.componentInstance.drafts.set(items);
    await settle();
    return fixture.nativeElement as HTMLElement;
  }

  const button = (el: HTMLElement, text: string) =>
    [...el.querySelectorAll<HTMLButtonElement>('article button')].find((b) =>
      b.textContent?.trim().startsWith(text),
    )!;

  it('shows each suggested order as a ticket with its source', async () => {
    setup();
    const el = await render([draft()]);
    const ticket = el.querySelector('article.ticket')!;
    expect(ticket.textContent).toContain('Buy');
    expect(ticket.textContent).toContain('AAA.US');
    expect(ticket.textContent).toContain('Tess book');
    expect(ticket.textContent).toContain('$125.00');
    expect(ticket.textContent).toContain('momentum turned up');
    expect(ticket.textContent).toContain('Suggested by the assistant');
    expect(ticket.textContent).toContain('PAPER');
    expect(ticket.classList).not.toContain('live');
  });

  it('approves with a fresh code and the ticket, then asks for a new read', async () => {
    setup();
    const toast = vi.spyOn(TestBed.inject(ToastService), 'success');
    const el = await render([draft()]);
    button(el, 'Approve').click();
    const req = await nextRequest(http, '/api/orders/drafts/od_1/approve', 'POST');
    expect(ensure).toHaveBeenCalled();
    expect(confirm).toHaveBeenCalledWith(
      expect.objectContaining({
        confirmLabel: 'Approve and place',
        tone: 'default',
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
    await settle();
    expect(toast).toHaveBeenCalledWith('Placed and filled: buy 5 AAA.US.');
    expect(fixture.componentInstance.changes).toBe(1);
  });

  it('types the ticker for a real-money book and shows a refusal on the card', async () => {
    setup('live');
    const el = await render([draft()]);
    expect(el.querySelector('article.ticket')?.classList).toContain('live');
    button(el, 'Approve').click();
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
    await settle();
    expect(el.querySelector('.failure')?.textContent).toContain('Not placed');
  });

  it('asks as for real money when the portfolio is not in the list', async () => {
    // The list failed to load, or the draft's portfolio is missing from it:
    // never confirm a possibly real-money order as paper.
    setup();
    const el = await render([draft({ portfolio_id: 'pf_unknown' })]);
    button(el, 'Approve').click();
    const req = await nextRequest(http, '/api/orders/drafts/od_1/approve', 'POST');
    expect(confirm).toHaveBeenCalledWith(
      expect.objectContaining({
        typedConfirmation: 'AAA.US',
        ticket: expect.objectContaining({ live: true }),
      }),
    );
    req.flush({}, { status: 409, statusText: 'Conflict' });
    await settle();
  });

  it('does nothing without the code', async () => {
    setup();
    const el = await render([draft()]);
    ensure.mockResolvedValueOnce(false);
    button(el, 'Approve').click();
    await tick(5);
    expect(confirm).not.toHaveBeenCalled();
    expect(http.match(() => true)).toHaveLength(0);
  });

  it('rejects with an optional note', async () => {
    setup();
    const el = await render([draft()]);
    button(el, 'Reject').click();
    await settle();
    // A paper order: no red button.
    const form = dialogForm(fixture.nativeElement as HTMLElement)!;
    expect(confirmButton(form).classList).not.toContain('btn-danger');
    answerDialog(fixture, { reason: 'not now' });
    const req = await nextRequest(http, '/api/orders/drafts/od_1/reject', 'POST');
    expect(req.request.body).toEqual({ note: 'not now' });
    req.flush(draft({ status: 'rejected' }));
    await settle();
    expect(fixture.componentInstance.changes).toBe(1);
  });

  it('shows no buttons on a decided order', async () => {
    setup();
    const el = await render([draft({ status: 'expired' })]);
    expect(el.textContent).toContain('Expired');
    expect(el.querySelectorAll('article button').length).toBe(0);
  });
});

describe('suggested order words', () => {
  it('names statuses and sources', () => {
    expect(draftStatus('pending').label).toBe('Waiting for you');
    expect(draftSource('mcp')).toBe('an agent tool');
  });
});

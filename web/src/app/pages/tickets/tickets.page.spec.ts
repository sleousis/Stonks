import { HttpTestingController, provideHttpClientTesting } from '@angular/common/http/testing';
import { type ComponentFixture, TestBed } from '@angular/core/testing';
import { provideRouter } from '@angular/router';

import { provideApi } from '../../api/provide-api';
import type { TicketView } from '../../api/tickets.service';
import { SessionService } from '../../core/auth/session.service';
import { StepUpService } from '../../core/auth/step-up.service';
import { ConfirmService } from '../../core/confirm/confirm.service';
import { ToastService } from '../../core/notify/toast.service';
import { TRADER } from '../../../testing/auth-fixtures';
import { nextRequest, page, tick } from '../../../testing/http';
import { TicketsPage } from './tickets.page';

function ticket(over: Partial<TicketView> = {}): TicketView {
  return {
    id: 'tkt_0000000000000001',
    portfolio_id: 'pf_live',
    portfolio_name: 'Growth',
    tick_id: 'tick_1',
    as_of: '2026-03-17',
    client_id: '2026-03-17:pf_live:momentum_1a2b3c4d:AAPL.US:buy',
    strategy_id: 'momentum_1a2b3c4d',
    ticker: 'AAPL.US',
    side: 'buy',
    quantity: 5,
    order_type: 'market',
    limit_price: null,
    position_effect: 'open',
    reference_price: 200,
    notional: 1000,
    what_if: null,
    reason: { score: 0.42, strategy_id: 'momentum_1a2b3c4d' },
    rules: [{ rule: 'max_weight_per_ticker', reason: 'cut to 10% of the book' }],
    hold: 'approve_mode',
    status: 'awaiting_approval',
    submit_after: '2026-03-18T13:10:00Z',
    expires_at: '2026-03-18T13:28:00Z',
    decided_by: null,
    decided_at: null,
    decision_reason: null,
    submitted_at: null,
    status_reason: null,
    created_at: '2026-03-17T20:45:00Z',
    ...over,
  };
}

const MSFT = ticket({
  id: 'tkt_0000000000000002',
  ticker: 'MSFT.US',
  client_id: '2026-03-17:pf_live:momentum_1a2b3c4d:MSFT.US:buy',
  notional: 500,
});

describe('TicketsPage', () => {
  let fixture: ComponentFixture<TicketsPage>;
  let controller: HttpTestingController;

  beforeEach(async () => {
    TestBed.configureTestingModule({
      providers: [provideRouter([]), ...provideApi(), provideHttpClientTesting()],
    });
    controller = TestBed.inject(HttpTestingController);
    const session = TestBed.inject(SessionService);
    const loading = session.load();
    (await nextRequest(controller, '/api/auth/me')).flush({ ...TRADER, via: 'session' });
    await loading;
  });

  afterEach(() => controller.verify());

  async function render(items: TicketView[]) {
    fixture = TestBed.createComponent(TicketsPage);
    fixture.detectChanges();
    (await nextRequest(controller, '/api/tickets')).flush(page(items));
    await tick();
    fixture.detectChanges();
    return fixture.nativeElement as HTMLElement;
  }

  function button(el: HTMLElement, text: string, index = 0) {
    return [...el.querySelectorAll<HTMLButtonElement>('button')].filter((b) =>
      b.textContent!.trim().startsWith(text),
    )[index];
  }

  it('shows each waiting order as a live ticket, grouped by run', async () => {
    const el = await render([ticket(), MSFT]);
    expect(el.querySelector('h2')!.textContent).toContain('Growth');
    expect(el.textContent).toContain('2 orders wait for you');
    const cards = el.querySelectorAll('article.ticket');
    expect(cards.length).toBe(2);
    expect(cards[0].classList.contains('live')).toBe(true);
    expect(cards[0].textContent).toContain('AAPL.US');
    expect(cards[0].textContent).toContain('At the open');
    expect(cards[0].textContent).toContain('You approve each trade of this strategy.');
    expect(cards[0].textContent).toContain('Checked by 1 rule');
    expect(cards[0].querySelector('app-mode-stamp')!.textContent).toContain('LIVE');
    expect(button(el, 'Approve all 2')).toBeTruthy();
  });

  it('says so when nothing waits', async () => {
    const el = await render([ticket({ status: 'filled', hold: null })]);
    expect(el.textContent).toContain('Nothing waits for you');
  });

  it('approves one ticket after a fresh code', async () => {
    const stepUp = vi.spyOn(TestBed.inject(StepUpService), 'ensure').mockResolvedValue(true);
    const success = vi.spyOn(TestBed.inject(ToastService), 'success');
    const el = await render([ticket(), MSFT]);
    button(el, 'Approve', 1).click();
    const req = await nextRequest(controller, '/api/tickets/approve', 'POST');
    expect(stepUp).toHaveBeenCalledWith('Approve Buy AAPL.US.');
    expect(req.request.body).toEqual({ ticket_ids: ['tkt_0000000000000001'] });
    req.flush({ items: [ticket({ status: 'approved', decided_by: 'user:usr_t' })] });
    await tick();
    fixture.detectChanges();
    expect(el.querySelectorAll('article.ticket').length).toBe(1);
    expect(success).toHaveBeenCalledWith(expect.stringContaining('before the open'));
  });

  it('sends nothing when the code is cancelled', async () => {
    vi.spyOn(TestBed.inject(StepUpService), 'ensure').mockResolvedValue(false);
    const el = await render([ticket()]);
    button(el, 'Approve').click();
    await tick();
    controller.expectNone('/api/tickets/approve');
  });

  it('approves a whole run with one ticket and one code', async () => {
    vi.spyOn(TestBed.inject(StepUpService), 'ensure').mockResolvedValue(true);
    const confirm = vi.spyOn(TestBed.inject(ConfirmService), 'confirm').mockResolvedValue(true);
    const el = await render([ticket(), MSFT]);
    button(el, 'Approve all 2').click();
    const req = await nextRequest(controller, '/api/tickets/approve', 'POST');
    const options = confirm.mock.calls[0][0];
    expect(options.ticket?.live).toBe(true);
    expect(options.ticket?.lines.find((l) => l.label === 'Orders')?.value).toBe('2');
    expect(req.request.body).toEqual({
      ticket_ids: ['tkt_0000000000000001', 'tkt_0000000000000002'],
    });
    req.flush({
      items: [ticket({ status: 'approved' }), { ...MSFT, status: 'approved' }],
    });
    await tick();
    fixture.detectChanges();
    expect(el.textContent).toContain('Nothing waits for you');
  });

  it('rejects with a reason', async () => {
    const el = await render([ticket()]);
    button(el, 'Reject').click();
    await tick();
    fixture.detectChanges();
    const dialog = el.querySelector('dialog')!;
    const submit = [...dialog.querySelectorAll<HTMLButtonElement>('button')].find(
      (b) => b.textContent!.trim() === 'Reject',
    )!;
    submit.click();
    fixture.detectChanges();
    expect(dialog.textContent).toContain('Say why');
    controller.expectNone('/api/tickets/tkt_0000000000000001/reject');

    const area = dialog.querySelector<HTMLTextAreaElement>('textarea')!;
    area.value = 'too big today';
    area.dispatchEvent(new Event('input'));
    fixture.detectChanges();
    submit.click();
    const req = await nextRequest(controller, '/api/tickets/tkt_0000000000000001/reject', 'POST');
    expect(req.request.body).toEqual({ reason: 'too big today' });
    req.flush(ticket({ status: 'rejected', decision_reason: 'too big today' }));
    await tick();
    fixture.detectChanges();
    expect(el.textContent).toContain('Nothing waits for you');
  });

  it('lists what became of every ticket in History', async () => {
    const el = await render([
      ticket({ status: 'filled', hold: null, decided_by: 'service:system' }),
      { ...MSFT, status: 'expired' },
    ]);
    el.querySelector<HTMLButtonElement>('button[role="radio"]:not([aria-checked="true"])')!.click();
    fixture.detectChanges();
    expect(el.textContent).toContain('Filled');
    expect(el.textContent).toContain('Expired');
    expect(el.textContent).toContain('Auto');
  });
});

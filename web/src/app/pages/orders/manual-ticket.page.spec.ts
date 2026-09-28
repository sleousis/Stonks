import type { Mock } from 'vitest';
import { HttpTestingController, provideHttpClientTesting } from '@angular/common/http/testing';
import { computed, signal } from '@angular/core';
import { type ComponentFixture, TestBed } from '@angular/core/testing';
import { provideRouter } from '@angular/router';

import type { ManualOrderResult } from '../../api/models';
import { CalendarsService } from '../../api/calendars.service';
import { provideApi } from '../../api/provide-api';
import { SessionService } from '../../core/auth/session.service';
import { StepUpService } from '../../core/auth/step-up.service';
import { type ConfirmOptions, ConfirmService } from '../../core/confirm/confirm.service';
import { ToastService } from '../../core/notify/toast.service';
import { PortfolioContextService } from '../../core/portfolio/portfolio-context.service';
import { nextRequest, tick } from '../../../testing/http';
import { book } from '../../../testing/portfolio-fixtures';
import { ManualTicketPage, orderLines } from './manual-ticket.page';
import { provideFakeCalendars } from '../../../testing/fake-calendars';
import { provideFakeTax } from '../../../testing/fake-tax';

function result(over: Partial<ManualOrderResult> = {}): ManualOrderResult {
  return {
    client_id: 'manual:pf_1:k1',
    portfolio_id: 'pf_1',
    ticker: 'AAA.US',
    side: 'buy',
    quantity: 10,
    requested_quantity: 10,
    order_type: 'market',
    limit_price: null,
    reference_price: 25,
    status: 'preview',
    live: false,
    adjustments: [],
    halt: null,
    ...over,
  };
}

const EMPTY_PAGE = { items: [], total: 0, limit: 50, offset: 0 };

describe('ManualTicketPage', () => {
  let fixture: ComponentFixture<ManualTicketPage>;
  let http: HttpTestingController;
  let confirm: Mock<(o: ConfirmOptions) => Promise<boolean>>;
  let ensure: Mock<(reason?: string) => Promise<boolean>>;
  const live = signal(false);

  function setup(isLive = false) {
    live.set(isLive);
    const current = signal(
      book({ id: 'pf_1', name: 'Tess book', trading: isLive ? 'live' : 'paper' }),
    );
    ensure = vi.fn(async () => true);
    TestBed.configureTestingModule({
      providers: [
        provideRouter([]),
        ...provideApi(),
        provideHttpClientTesting(),
        provideFakeTax(),
        provideFakeCalendars(),
        {
          provide: PortfolioContextService,
          useValue: {
            current,
            options: computed(() => [current()]),
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
    confirm = vi.fn<(o: ConfirmOptions) => Promise<boolean>>(async () => true);
    vi.spyOn(TestBed.inject(ConfirmService), 'confirm').mockImplementation(confirm);
  }

  afterEach(() => http.verify());

  async function render(inputs: Record<string, string> = {}) {
    fixture = TestBed.createComponent(ManualTicketPage);
    for (const [k, v] of Object.entries(inputs)) fixture.componentRef.setInput(k, v);
    fixture.detectChanges();
    (await nextRequest(http, '/api/orders')).flush(EMPTY_PAGE);
    await settle();
    return fixture.nativeElement as HTMLElement;
  }

  async function settle() {
    for (let i = 0; i < 3; i++) {
      await tick();
      fixture.detectChanges();
    }
  }

  function type(el: HTMLElement, selector: string, value: string) {
    const input = el.querySelector<HTMLInputElement | HTMLTextAreaElement>(selector)!;
    input.value = value;
    input.dispatchEvent(new Event('input'));
    fixture.detectChanges();
  }

  function pick(el: HTMLElement, group: string, label: string) {
    const radios = el.querySelectorAll<HTMLButtonElement>(
      `[role="radiogroup"][aria-label="${group}"] [role="radio"]`,
    );
    [...radios].find((r) => r.textContent?.trim() === label)!.click();
    fixture.detectChanges();
  }

  function fill(el: HTMLElement) {
    type(el, '#mo-ticker', 'aaa.us');
    type(el, '#mo-qty', '10');
    type(el, '#mo-reason', 'earnings dip');
  }

  const button = (el: HTMLElement, name: string) =>
    [...el.querySelectorAll<HTMLButtonElement>('button')].find(
      (b) => b.textContent?.trim() === name,
    )!;

  it('shows the picked side clearly: a solid choice and a line in words (M13)', async () => {
    setup();
    const el = await render();
    const side = el.querySelector('[role="radiogroup"][aria-label="Side"]')!;
    expect(side.getAttribute('data-emphasis')).toBe('strong');
    expect(el.querySelector('.side-now')!.textContent).toContain('You are buying');
    pick(el, 'Side', 'Sell');
    expect(el.querySelector('.side-now')!.textContent).toContain('You are selling');
    expect(el.querySelector('.side-now app-side-tag')!.textContent).toContain('Sell');
  });

  it('says what is missing and sends nothing', async () => {
    setup();
    const el = await render();
    button(el, 'Check order').click();
    fixture.detectChanges();
    expect(el.querySelector('#mo-ticker-error')?.textContent).toContain('Enter a ticker');
    expect(el.querySelector('#mo-qty-error')?.textContent).toContain('above zero');
    expect(el.querySelector('#mo-reason-error')).not.toBeNull();
    await tick(5);
    expect(http.match(() => true)).toHaveLength(0);
  });

  it('works out a whole-share size from the risk and the stop', async () => {
    setup();
    const el = await render();
    fill(el);
    type(el, '#mo-stop', '24');
    type(el, '#mo-target', '28');
    type(el, '#mo-risk', '1');
    button(el, 'Work out size').click();
    const req = await nextRequest(http, '/api/orders/manual/plan', 'POST');
    expect(req.request.body).toMatchObject({
      portfolio_id: 'pf_1',
      ticker: 'AAA.US',
      side: 'buy',
      stop_price: 24,
      target_price: 28,
      risk_percent: 1,
      risk_amount: null,
    });
    req.flush({
      ticker: 'AAA.US',
      side: 'buy',
      entry_price: 25,
      entry_is_close: true,
      stop_price: 24,
      target_price: 28,
      equity: 10_000,
      cash: 10_000,
      risk_budget: 100,
      risk_per_share: 1,
      quantity: 100,
      risk_amount: 100,
      notional: 2_500,
      reward_risk: 3,
      capped_by: null,
      note: null,
    });
    await settle();
    expect(el.querySelector<HTMLInputElement>('#mo-qty')!.value).toBe('100');
    expect(el.querySelector('#mo-plan-result')?.textContent).toContain('3 to 1');
    button(el, 'Check order').click();
    const check = await nextRequest(http, '/api/orders/manual/preview', 'POST');
    expect(check.request.body).toMatchObject({ quantity: 100, stop_price: 24, target_price: 28 });
    check.flush(result({ quantity: 100, requested_quantity: 100, stop_price: 24, reward_risk: 3 }));
    await settle();
    const ticket = el.querySelector('[aria-label="Checked order"]')!;
    expect(ticket.textContent).toContain('Stop');
    expect(ticket.textContent).toContain('3 to 1');
  });

  it('drops a size worked out for a stop that changed meanwhile', async () => {
    setup();
    const el = await render();
    fill(el);
    type(el, '#mo-stop', '24');
    type(el, '#mo-risk', '1');
    const qtyBefore = el.querySelector<HTMLInputElement>('#mo-qty')!.value;
    button(el, 'Work out size').click();
    const req = await nextRequest(http, '/api/orders/manual/plan', 'POST');
    // The trader moves the stop while the size is on its way.
    type(el, '#mo-stop', '20');
    req.flush({
      ticker: 'AAA.US',
      side: 'buy',
      entry_price: 25,
      entry_is_close: true,
      stop_price: 24,
      target_price: null,
      equity: 10_000,
      cash: 10_000,
      risk_budget: 100,
      risk_per_share: 1,
      quantity: 100,
      risk_amount: 100,
      notional: 2_500,
      reward_risk: null,
      capped_by: null,
      note: null,
    });
    await settle();
    expect(el.querySelector<HTMLInputElement>('#mo-qty')!.value).toBe(qtyBefore);
    expect(el.querySelector('#mo-plan-result')?.textContent).not.toContain('to 1');
    expect(el.querySelector('#mo-plan-result')?.textContent).toContain('whole shares');
  });

  it('asks for the stop and the risk before sizing', async () => {
    setup();
    const el = await render();
    type(el, '#mo-ticker', 'aaa.us');
    button(el, 'Work out size').click();
    fixture.detectChanges();
    expect(el.querySelector('#mo-stop-error')?.textContent).toContain('stop');
    expect(el.querySelector('#mo-risk-error')?.textContent).toContain('risk');
    await tick(5);
    expect(http.match(() => true)).toHaveLength(0);
  });

  it('checks the order and shows it as a ticket', async () => {
    setup();
    const el = await render();
    fill(el);
    pick(el, 'Order type', 'Limit');
    type(el, '#mo-limit', '24.5');
    button(el, 'Check order').click();
    const req = await nextRequest(http, '/api/orders/manual/preview', 'POST');
    expect(req.request.body).toMatchObject({
      portfolio_id: 'pf_1',
      ticker: 'AAA.US',
      side: 'buy',
      quantity: 10,
      order_type: 'limit',
      limit_price: 24.5,
      reason: 'earnings dip',
      allow_reduce: false,
    });
    expect(req.request.body.client_id).toMatch(/^console-/);
    req.flush(result({ order_type: 'limit', limit_price: 24.5 }));
    await settle();
    const ticket = el.querySelector('[aria-label="Checked order"]')!;
    expect(ticket.textContent).toContain('Tess book');
    expect(ticket.textContent).toContain('Limit at $24.50');
    expect(ticket.textContent).toContain('PAPER');
    expect(ticket.textContent).toContain('Every check passed');
  });

  it('places a paper order after the ticket, then reads the list again', async () => {
    setup();
    const toast = vi.spyOn(TestBed.inject(ToastService), 'success');
    const el = await render();
    fill(el);
    pick(el, 'Side', 'Sell');
    button(el, 'Place order').click();
    const preview = await nextRequest(http, '/api/orders/manual/preview', 'POST');
    const key = preview.request.body.client_id;
    preview.flush(result({ side: 'sell' }));
    const placed = await nextRequest(http, '/api/orders/manual', 'POST');
    expect(ensure).not.toHaveBeenCalled();
    expect(confirm).toHaveBeenCalledWith(
      expect.objectContaining({
        title: 'Sell 10 AAA.US?',
        confirmLabel: 'Place order',
        typedConfirmation: undefined,
        ticket: expect.objectContaining({ side: 'sell', live: false }),
      }),
    );
    expect(placed.request.body).toMatchObject({ side: 'sell', client_id: key });
    placed.flush(result({ side: 'sell', status: 'filled', fill_price: 25 }));
    (await nextRequest(http, '/api/orders')).flush(EMPTY_PAGE);
    await settle();
    expect(toast).toHaveBeenCalledWith('Sold 10 AAA.US at $25.00.');
    expect(el.querySelector('.result')?.textContent).toContain('Filled at $25.00');
    expect(el.querySelector<HTMLInputElement>('#mo-qty')!.value).toBe('');
  });

  it('does not place when the ticket is cancelled', async () => {
    setup();
    const el = await render();
    fill(el);
    confirm.mockResolvedValueOnce(false);
    button(el, 'Place order').click();
    (await nextRequest(http, '/api/orders/manual/preview', 'POST')).flush(result());
    await tick(5);
    expect(http.match(() => true)).toHaveLength(0);
  });

  it('shows a refusal with each rule and checks again with a smaller order', async () => {
    setup();
    const el = await render();
    fill(el);
    button(el, 'Check order').click();
    (await nextRequest(http, '/api/orders/manual/preview', 'POST')).flush(
      {
        title: 'Conflict',
        status: 409,
        code: 'order_refused',
        detail: 'the risk rules allow 4 of the 10 asked for',
        risk_adjustments: [
          {
            rule: 'max_position_weight',
            ticker: 'AAA.US',
            side: 'buy',
            original_quantity: 10,
            adjusted_quantity: 4,
            reason: 'caps the holding',
          },
        ],
      },
      { status: 409, statusText: 'Conflict' },
    );
    await settle();
    const alert = el.querySelector('app-order-refusal [role="alert"]')!;
    expect(alert.textContent).toContain('allow 4 of the 10');
    expect(alert.textContent).toContain('Max position weight');
    alert.querySelector<HTMLInputElement>('input[type="checkbox"]')!.click();
    const again = await nextRequest(http, '/api/orders/manual/preview', 'POST');
    expect(again.request.body.allow_reduce).toBe(true);
    again.flush(result({ quantity: 4, requested_quantity: 10 }));
    await settle();
    expect(el.textContent).toContain('The risk limits cut this order to 4');
    expect(el.querySelector('[aria-label="Checked order"]')?.textContent).toContain('Asked for');
  });

  it('asks for a fresh code and the typed ticker on a real-money book', async () => {
    setup(true);
    const el = await render();
    fill(el);
    button(el, 'Place order').click();
    (await nextRequest(http, '/api/orders/manual/preview', 'POST')).flush(result({ live: true }));
    const placed = await nextRequest(http, '/api/orders/manual', 'POST');
    expect(ensure).toHaveBeenCalledWith(expect.stringContaining('real-money'));
    expect(confirm).toHaveBeenCalledWith(
      expect.objectContaining({
        tone: 'danger',
        typedConfirmation: 'AAA.US',
        ticket: expect.objectContaining({ live: true }),
      }),
    );
    placed.flush(result({ live: true, status: 'pending' }));
    (await nextRequest(http, '/api/orders')).flush(EMPTY_PAGE);
    await settle();
    expect(el.querySelector('.stamp')?.textContent).toContain('LIVE');
  });

  it('stops when the code is not confirmed', async () => {
    setup(true);
    const el = await render();
    fill(el);
    ensure.mockResolvedValueOnce(false);
    button(el, 'Place order').click();
    await tick(5);
    expect(http.match(() => true)).toHaveLength(0);
    expect(confirm).not.toHaveBeenCalled();
  });

  it('shows any other failure inline', async () => {
    setup();
    const el = await render();
    fill(el);
    button(el, 'Check order').click();
    (await nextRequest(http, '/api/orders/manual/preview', 'POST')).flush(
      { title: 'Not Found', status: 404, detail: 'no price for AAA.US' },
      { status: 404, statusText: 'Not Found' },
    );
    await settle();
    expect(el.querySelector('.failure')?.textContent).toContain('no price for AAA.US');
  });

  it('starts from the ticker and side in the address', async () => {
    setup();
    const el = await render({ ticker: 'bbb.us', side: 'sell' });
    expect(el.querySelector<HTMLInputElement>('#mo-ticker')!.value).toBe('BBB.US');
    const sell = el.querySelector('[aria-label="Side"] [aria-checked="true"]');
    expect(sell?.textContent?.trim()).toBe('Sell');
  });

  it('warns on the ticket when earnings fall before the next open', async () => {
    setup();
    const asked: string[][] = [];
    vi.spyOn(TestBed.inject(CalendarsService), 'earningsWarnings').mockImplementation(
      async (tickers: readonly string[]) => {
        asked.push([...tickers]);
        return {
          checked: [...tickers],
          warnings: [
            {
              ticker: 'AAA.US',
              report_date: '2026-10-29',
              before_after_market: 'before',
              next_open: '2026-10-29T13:30:00Z',
            },
          ],
        };
      },
    );
    const el = await render({ ticker: 'aaa.us' });
    await settle();
    expect(asked).toContainEqual(['AAA.US']);
    expect(el.querySelector('app-earnings-warning')?.textContent).toContain(
      'Earnings before the next open.',
    );
  });
});

describe('orderLines', () => {
  it('shows the smaller quantity next to the ask', () => {
    const lines = orderLines(result({ quantity: 4, requested_quantity: 10 }), 'Book', 'USD');
    expect(lines.map((l) => l.label)).toEqual([
      'Portfolio',
      'Ticker',
      'Quantity',
      'Asked for',
      'Type',
      'Last close',
      'About',
    ]);
    expect(lines.find((l) => l.label === 'About')?.value).toBe('$100.00');
  });
});

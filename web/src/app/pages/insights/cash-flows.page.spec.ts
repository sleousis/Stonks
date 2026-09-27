import {
  HttpTestingController,
  type TestRequest,
  provideHttpClientTesting,
} from '@angular/common/http/testing';
import { signal } from '@angular/core';
import { type ComponentFixture, TestBed } from '@angular/core/testing';
import { provideRouter } from '@angular/router';

import type { CashFlowView } from '../../api/models';
import { provideApi } from '../../api/provide-api';
import { SessionService } from '../../core/auth/session.service';
import { ConfirmService } from '../../core/confirm/confirm.service';
import { PortfolioContextService } from '../../core/portfolio/portfolio-context.service';
import { nextRequest, page, tick } from '../../../testing/http';
import { book } from '../../../testing/portfolio-fixtures';
import { CashFlowsPage, signedAmount } from './cash-flows.page';
import { INSIGHTS } from './insights.fixtures';

const FLOWS: CashFlowView[] = [
  {
    id: 1,
    portfolio_id: 'pf_1',
    flow_date: '2026-09-01',
    kind: 'deposit',
    amount: 5000,
    source: 'manual',
    note: 'Top up',
  },
  {
    id: null,
    portfolio_id: 'pf_1',
    flow_date: '2026-09-10',
    kind: 'withdrawal',
    amount: 250,
    source: 'broker',
  },
];

describe('CashFlowsPage', () => {
  let fixture: ComponentFixture<CashFlowsPage>;
  let http: HttpTestingController;
  let el: HTMLElement;
  let confirm: ReturnType<typeof vi.fn>;
  let allowed: boolean;
  const current = signal(book({ id: 'pf_1', name: 'Main', is_default: true }));

  beforeEach(() => {
    allowed = true;
    confirm = vi.fn().mockResolvedValue(true);
    current.set(book({ id: 'pf_1', name: 'Main', is_default: true }));
    TestBed.configureTestingModule({
      providers: [
        ...provideApi(),
        provideHttpClientTesting(),
        provideRouter([]),
        {
          provide: PortfolioContextService,
          useValue: {
            selectedId: () => 'pf_1',
            current,
            live: () => false,
            state: () => 'ready',
            noBook: () => false,
            query: () => ({ portfolio_id: 'pf_1' }),
          },
        },
        {
          provide: SessionService,
          useValue: { can: () => allowed, whyNot: () => null, csrfToken: () => null },
        },
        { provide: ConfirmService, useValue: { confirm } },
      ],
    });
    http = TestBed.inject(HttpTestingController);
  });

  afterEach(() => http.verify());

  function respond(req: TestRequest, flows = FLOWS): void {
    const path = new URL(req.request.urlWithParams, 'http://x').pathname;
    if (path === '/api/insights') return req.flush(INSIGHTS);
    if (path === '/api/portfolios/pf_1/cash-flows') return req.flush(page(flows));
    throw new Error(`unexpected ${path}`);
  }

  async function flushAll(flows = FLOWS): Promise<void> {
    for (let i = 0; i < 5; i++) {
      http.match((r) => r.method === 'GET').forEach((r) => respond(r, flows));
      await tick(5);
      fixture.detectChanges();
    }
  }

  async function render(flows = FLOWS): Promise<void> {
    fixture = TestBed.createComponent(CashFlowsPage);
    el = fixture.nativeElement;
    fixture.detectChanges();
    await flushAll(flows);
  }

  function type(selector: string, value: string): void {
    const input = el.querySelector<HTMLInputElement>(selector)!;
    input.value = value;
    input.dispatchEvent(new Event('input'));
    fixture.detectChanges();
  }

  it('shows the returns without flows and every deposit and withdrawal, newest first', async () => {
    await render();
    const tiles = el.querySelector('.tiles')!.textContent!;
    expect(tiles).toContain('Time-weighted return');
    expect(tiles).toContain('Money-weighted return');
    expect(tiles).toContain('+8.5%');
    expect(tiles).toContain('+$5,000.00');
    const rows = [...el.querySelectorAll('tbody tr')].map((r) => r.textContent ?? '');
    expect(rows[0]).toContain('Withdrawal');
    expect(rows[0]).toContain('-$250.00');
    expect(rows[0]).toContain('Broker sync');
    expect(rows[1]).toContain('+$5,000.00');
    expect(rows[1]).toContain('Recorded here');
  });

  it('records a deposit after a ticket, then reloads', async () => {
    await render();
    type('#flow-amount', '1500');
    type('#flow-note', ' bonus ');
    el.querySelector<HTMLFormElement>('form')!.dispatchEvent(new Event('submit'));
    await tick();
    expect(confirm).toHaveBeenCalledWith(
      expect.objectContaining({
        confirmLabel: 'Record deposit',
        ticket: expect.objectContaining({ kind: 'Deposit', live: false }),
      }),
    );
    const req = await nextRequest(http, '/api/portfolios/pf_1/cash-flows', 'POST');
    expect(req.request.body).toEqual({
      kind: 'deposit',
      amount: 1500,
      flow_date: null,
      note: 'bonus',
    });
    req.flush({ ...FLOWS[0], id: 3, amount: 1500 });
    await flushAll();
    expect(el.querySelector<HTMLInputElement>('#flow-amount')!.value).toBe('');
  });

  it('records a withdrawal on a picked date', async () => {
    await render();
    const withdrawal = [...el.querySelectorAll<HTMLButtonElement>('[role=radio]')].find(
      (b) => b.textContent?.trim() === 'Withdrawal',
    )!;
    withdrawal.click();
    fixture.detectChanges();
    type('#flow-amount', '200');
    type('#flow-date', '2026-09-15');
    el.querySelector<HTMLFormElement>('form')!.dispatchEvent(new Event('submit'));
    await tick();
    const req = await nextRequest(http, '/api/portfolios/pf_1/cash-flows', 'POST');
    expect(req.request.body).toMatchObject({
      kind: 'withdrawal',
      amount: 200,
      flow_date: '2026-09-15',
    });
    req.flush({ ...FLOWS[1], id: 4 });
    await flushAll();
  });

  it('checks the amount before asking', async () => {
    await render();
    type('#flow-amount', '-5');
    el.querySelector<HTMLFormElement>('form')!.dispatchEvent(new Event('submit'));
    fixture.detectChanges();
    expect(el.textContent).toContain('Enter an amount above 0.');
    expect(confirm).not.toHaveBeenCalled();
  });

  it('sends nothing when the ticket is cancelled', async () => {
    confirm.mockResolvedValue(false);
    await render();
    type('#flow-amount', '10');
    el.querySelector<HTMLFormElement>('form')!.dispatchEvent(new Event('submit'));
    await tick();
    http.expectNone({ method: 'POST' });
  });

  it('explains that a broker book gets its flows from the sync', async () => {
    current.set(book({ id: 'pf_1', name: 'Broker', kind: 'broker', trading: 'live' }));
    await render([]);
    expect(el.querySelector('#flow-amount')).toBeNull();
    expect(el.textContent).toContain('come in with each broker sync');
    expect(el.textContent).toContain('No deposits or withdrawals yet');
  });

  it('writes a withdrawal as money going out', () => {
    expect(signedAmount({ kind: 'withdrawal', amount: 5 })).toBe(-5);
    expect(signedAmount({ kind: 'deposit', amount: 5 })).toBe(5);
  });
});

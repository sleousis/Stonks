import {
  HttpTestingController,
  type TestRequest,
  provideHttpClientTesting,
} from '@angular/common/http/testing';
import { type ComponentFixture, TestBed } from '@angular/core/testing';
import { provideRouter } from '@angular/router';

import { TRADER } from '../../../testing/auth-fixtures';
import { provideFakeChart } from '../../../testing/fake-chart';
import { nextRequest, tick } from '../../../testing/http';
import { provideApi } from '../../api/provide-api';
import { SessionService } from '../../core/auth/session.service';
import { JOB_FETCH, JOB_POLL_MS } from '../../core/jobs/jobs.service';
import {
  BACKTEST,
  CHAIN,
  PAYOFF,
  STRATEGIES,
  STRUCTURES,
  UNDERLYING,
  job,
} from './options-test-fixtures';
import { OptionsPage } from './options.page';
import { provideFakeDataCoverage } from '../../../testing/fake-data-coverage';

describe('OptionsPage', () => {
  let fixture: ComponentFixture<OptionsPage>;
  let controller: HttpTestingController;
  let el: HTMLElement;
  let posted: Record<string, unknown>;
  let chainQueries: string[];
  let underlyings: unknown[];
  let payoffFails: boolean;

  async function open(): Promise<void> {
    TestBed.configureTestingModule({
      imports: [OptionsPage],
      providers: [
        provideFakeDataCoverage(),
        ...provideApi(),
        provideHttpClientTesting(),
        provideRouter([]),
        provideFakeChart(),
        { provide: JOB_FETCH, useValue: () => Promise.reject(new Error('no stream')) },
        { provide: JOB_POLL_MS, useValue: 1 },
      ],
    });
    controller = TestBed.inject(HttpTestingController);
    const loading = TestBed.inject(SessionService).load();
    (await nextRequest(controller, '/api/auth/me')).flush(TRADER);
    await loading;
    fixture = TestBed.createComponent(OptionsPage);
    el = fixture.nativeElement;
    fixture.detectChanges();
    await settle();
  }

  beforeEach(() => {
    posted = {};
    chainQueries = [];
    underlyings = [UNDERLYING];
    payoffFails = false;
  });

  afterEach(() => controller.verify());

  function respond(req: TestRequest): void {
    const url = new URL(req.request.urlWithParams, 'http://localhost');
    const path = url.pathname;
    if (req.request.method === 'POST') {
      posted[path] = req.request.body;
      if (path === '/api/options/payoff') {
        if (payoffFails) {
          return req.flush(
            { title: 'Invalid request', detail: 'no liquid legs for covered_call', status: 422 },
            { status: 422, statusText: 'Unprocessable Entity' },
          );
        }
        return req.flush(PAYOFF);
      }
      if (path === '/api/options/backtests') {
        return req.flush(job('ob-1', { status: 'queued', progress: 0 }), {
          status: 202,
          statusText: 'Accepted',
        });
      }
    }
    switch (path) {
      case '/api/options/underlyings':
        return req.flush({ items: underlyings, total: underlyings.length, limit: 100, offset: 0 });
      case '/api/options/strategies':
        return req.flush(STRATEGIES);
      case '/api/options/structures':
        return req.flush(STRUCTURES);
      case '/api/options/chains/AAPL.US':
        chainQueries.push(url.search);
        return req.flush(CHAIN);
      case '/api/jobs/ob-1':
        return req.flush(job('ob-1'));
      case '/api/options/backtests/ob-1/result':
        return req.flush(BACKTEST);
    }
    throw new Error(`unexpected ${req.request.method} ${path}`);
  }

  async function settle(rounds = 12): Promise<void> {
    for (let i = 0; i < rounds; i++) {
      controller.match(() => true).forEach(respond);
      await tick(5);
      fixture.detectChanges();
    }
  }

  function panel(title: string): HTMLElement {
    const h = [...el.querySelectorAll('h2')].find((x) => x.textContent!.trim() === title);
    if (!h) throw new Error(`no panel ${title}`);
    return h.closest('section')!;
  }

  function button(root: HTMLElement, text: string): HTMLButtonElement {
    const b = [...root.querySelectorAll('button')].find((x) => x.textContent!.trim() === text);
    if (!b) throw new Error(`no button ${text}`);
    return b as HTMLButtonElement;
  }

  it('says it is research only and opens the first stored chain', async () => {
    await open();
    expect(el.querySelector('[role="note"]')!.textContent).toContain(
      'Research only, nothing on this page trades options.',
    );
    const chain = panel('Chain');
    expect(chain.textContent).toContain('Generated chains, not market quotes');
    expect(chain.textContent).toContain('Expiry 2026-04-17, 17 days');
    expect(chain.querySelector('caption')!.textContent).toContain('Option chain by strike');
    const headers = [...chain.querySelectorAll('th')].map((th) => th.textContent!.trim());
    expect(headers).toContain('Call bid');
    expect(headers).toContain('Put delta');
    expect(chain.textContent).toContain('Barone-Adesi Whaley (American)');
    expect(chainQueries).toEqual(['']);
  });

  it('asks for another date and expiry, and switches to puts only', async () => {
    await open();
    const chain = panel('Chain');
    const day = chain.querySelector<HTMLInputElement>('#op-day')!;
    day.value = '2026-02-02';
    day.dispatchEvent(new Event('change'));
    await settle();
    expect(chainQueries.at(-1)).toBe('?as_of=2026-02-02');
    const expiry = chain.querySelector<HTMLSelectElement>('#op-expiry')!;
    expiry.value = '2026-05-15';
    expiry.dispatchEvent(new Event('change'));
    await settle();
    expect(chainQueries.at(-1)).toBe('?as_of=2026-02-02&expiry=2026-05-15');
    const puts = [...chain.querySelectorAll<HTMLButtonElement>('[role="radio"]')].find(
      (b) => b.textContent!.trim() === 'Puts',
    )!;
    puts.click();
    fixture.detectChanges();
    const headers = [...chain.querySelectorAll('th')].map((th) => th.textContent!.trim());
    expect(headers).toContain('Delta');
    expect(headers).not.toContain('Call bid');
  });

  it('draws a payoff with the structure parameters', async () => {
    await open();
    const payoff = panel('Payoff at expiry');
    expect(payoff.querySelector<HTMLInputElement>('#op-param-dte')!.value).toBe('35');
    expect(payoff.querySelector('#op-param-short_delta')).not.toBeNull();
    const dte = payoff.querySelector<HTMLInputElement>('#op-param-dte')!;
    dte.value = '45';
    dte.dispatchEvent(new Event('input'));
    button(payoff, 'Draw payoff').click();
    await settle();
    expect(posted['/api/options/payoff']).toEqual({
      underlying: 'AAPL.US',
      as_of: null,
      structure: 'bull_call_spread',
      dte: 45,
      long_delta: 0.5,
      short_delta: 0.25,
    });
    expect(payoff.querySelector('app-payoff-diagram')).not.toBeNull();
    expect(payoff.textContent).toContain('Sell 1 call 110');
  });

  it('shows a payoff that cannot be built inline, and notes shares held', async () => {
    await open();
    payoffFails = true;
    const payoff = panel('Payoff at expiry');
    const structure = payoff.querySelector<HTMLSelectElement>('#op-structure')!;
    structure.value = 'covered_call';
    structure.dispatchEvent(new Event('change'));
    fixture.detectChanges();
    expect(payoff.textContent).toContain("Includes one contract's worth of shares held.");
    expect(payoff.querySelector('#op-param-delta')).not.toBeNull();
    button(payoff, 'Draw payoff').click();
    await settle();
    expect(payoff.textContent).toContain('Could not draw this payoff');
    expect(payoff.textContent).toContain('no liquid legs');
  });

  it('lists the strategies with their hypotheses', async () => {
    await open();
    const list = panel('Options strategies');
    expect(list.textContent).toContain('Cash secured put');
    expect(list.textContent).toContain('Bull call spread, Bear put spread');
    expect(list.textContent).toContain('Momentum picks the direction');
  });

  it('runs a backtest and shows its verdict and checks', async () => {
    await open();
    const bt = panel('Backtest');
    expect(bt.querySelector<HTMLInputElement>('#obt-underlyings')!.value).toBe('AAPL.US');
    expect(bt.querySelector<HTMLInputElement>('#obt-start')!.value).toBe('2026-01-02');
    button(bt, 'Run backtest').click();
    await settle();
    expect(bt.textContent).toContain('Pick a strategy.');
    expect(posted['/api/options/backtests']).toBeUndefined();

    const select = bt.querySelector<HTMLSelectElement>('#obt-strategy')!;
    select.value = 'cash_secured_put';
    select.dispatchEvent(new Event('change'));
    fixture.detectChanges();
    expect(bt.textContent).toContain('earns the volatility premium');
    button(bt, 'Run backtest').click();
    await settle(20);
    expect(posted['/api/options/backtests']).toEqual({
      strategy: 'cash_secured_put',
      underlyings: ['AAPL.US'],
      start: '2026-01-02',
      end: '2026-03-31',
      cash: 100000,
      params: { delta: 0.3 },
      validation: true,
    });
    expect(bt.textContent).toContain('Checks failed');
    expect(bt.textContent).toContain('never evidence');
    expect(bt.textContent).toContain('1.20%');
    expect(bt.textContent).toContain('n/a');
    const checks = [...bt.querySelectorAll('.checks li')].map((li) => li.textContent!);
    expect(checks[0]).toContain('Out of sample');
    expect(checks[1]).toContain('Doubled fees');
  });

  it('explains an empty lake without sending anyone to a terminal', async () => {
    underlyings = [];
    await open();
    expect(el.textContent).toContain('No option chains stored yet');
    expect(el.querySelector('app-options-backtest')).toBeNull();
  });
});

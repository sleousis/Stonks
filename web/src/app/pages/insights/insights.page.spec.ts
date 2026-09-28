import {
  HttpTestingController,
  type TestRequest,
  provideHttpClientTesting,
} from '@angular/common/http/testing';
import { signal } from '@angular/core';
import { type ComponentFixture, TestBed } from '@angular/core/testing';
import { provideRouter } from '@angular/router';

import { provideApi } from '../../api/provide-api';
import { SessionService } from '../../core/auth/session.service';
import { PortfolioContextService } from '../../core/portfolio/portfolio-context.service';
import { tick } from '../../../testing/http';
import { AGREEMENT, INSIGHTS, SNAPSHOTS, TOTALS } from './insights.fixtures';
import { InsightsPage, agreementLine } from './insights.page';

describe('InsightsPage', () => {
  let fixture: ComponentFixture<InsightsPage>;
  let http: HttpTestingController;
  let el: HTMLElement;
  let admin: boolean;
  let totalsBody: typeof TOTALS = TOTALS;
  const selected = signal<string | null>(null);
  const live = signal(false);
  let base = 'USD';
  const seen: string[] = [];

  function setup(): void {
    TestBed.configureTestingModule({
      imports: [InsightsPage],
      providers: [
        ...provideApi(),
        provideHttpClientTesting(),
        provideRouter([]),
        {
          provide: PortfolioContextService,
          useValue: {
            selectedId: selected,
            live,
            current: () => ({ base_currency: base }),
            state: () => 'ready',
            noBook: () => false,
            query: () => (selected() ? { portfolio_id: selected() } : {}),
          },
        },
        { provide: SessionService, useValue: { can: () => admin } },
      ],
    });
    http = TestBed.inject(HttpTestingController);
    fixture = TestBed.createComponent(InsightsPage);
    el = fixture.nativeElement;
    fixture.detectChanges();
  }

  beforeEach(() => {
    admin = false;
    selected.set(null);
    live.set(false);
    base = 'USD';
    seen.length = 0;
  });

  afterEach(() => http.verify());

  function respond(req: TestRequest): void {
    const url = new URL(req.request.urlWithParams, 'http://localhost');
    seen.push(url.pathname + url.search);
    switch (url.pathname) {
      case '/api/insights':
        return req.flush(INSIGHTS);
      case '/api/insights/agreement':
        return req.flush(AGREEMENT);
      case '/api/insights/totals':
        return req.flush(totalsBody);
      case '/api/portfolio/snapshots':
        return req.flush(SNAPSHOTS);
      default:
        throw new Error(`unexpected request ${url.pathname}`);
    }
  }

  async function flushAll(): Promise<void> {
    for (let i = 0; i < 6; i++) {
      http.match(() => true).forEach(respond);
      await tick(5);
      fixture.detectChanges();
    }
  }

  it('shows the value, exposure, allocation, returns and risk of the book', async () => {
    setup();
    await flushAll();
    const text = el.textContent ?? '';
    expect(el.querySelector('h1')?.textContent).toContain('Insights');
    expect(text).toContain('$100,000.00');
    expect(text).toContain('1.12');
    expect(text).toContain('Against SPY.US');
    // Asset classes read as words (M6).
    expect(text).toContain('Equity');
    expect(text).toContain('70.0%');
    expect(text).toContain('Since the start');
    // Money changes carry their sign (UX-58).
    expect(text).toContain('+$1,000.00 today');
    expect(el.querySelector('.figures dd.gain')?.textContent).toContain('+$1,000.00');
    expect(text).toContain('n/a');
    expect(text).toContain('22.0%');
    expect(text).toContain('Beta covers 90% of the holdings.');
    expect(el.querySelector('.tile-value .live, .tile-value.live')).toBeNull();
  });

  it('shows time and money weighted returns with deposits left out', async () => {
    setup();
    await flushAll();
    const returns = el.querySelector('section[aria-labelledby="returns-title"]')!.textContent!;
    expect(returns).toContain('Return +0.4%');
    expect(returns).toContain('Money-weighted, per year');
    expect(returns).toContain('+8.5%');
    expect(returns).toContain('+$5,000.00');
    expect(returns).toContain('Record a deposit or withdrawal');
    expect(el.textContent).not.toContain('Value in');
  });

  it('lays out the time-weighted monthly returns as a heatmap (13.7)', async () => {
    setup();
    await flushAll();
    const panel = el.querySelector('section[aria-labelledby="months-title"]')!;
    expect(panel.querySelector('caption')?.textContent).toContain('deposits and withdrawals');
    const cells = [...panel.querySelectorAll('tbody td')].map((c) => c.textContent?.trim());
    expect(cells.slice(6, 9)).toEqual(['+1.2%', '-3.1%', '+0.4%']);
    expect(cells.at(-1)).toBe('-1.5%'); // the year compounded
    expect(panel.querySelector('td[data-tone="loss"]')?.getAttribute('data-strength')).toBe('2');
  });

  it('says how monthly returns fill in before the first month is measured', async () => {
    setup();
    for (let i = 0; i < 6; i++) {
      http
        .match(() => true)
        .forEach((req) => {
          if (new URL(req.request.url, 'http://localhost').pathname === '/api/insights') {
            req.flush({ ...INSIGHTS, monthly_returns: [{ month: '2026-09', value: null }] });
          } else respond(req);
        });
      await tick(5);
      fixture.detectChanges();
    }
    const panel = el.querySelector('section[aria-labelledby="months-title"]')!;
    expect(panel.querySelector('table')).toBeNull();
    expect(panel.textContent).toContain('never count as return');
  });

  it('shows the value in the base currency when it differs', async () => {
    base = 'EUR';
    setup();
    await flushAll();
    expect(el.querySelector('.base-line')?.textContent).toContain('Value in EUR: €92,000.00');
  });

  it('switches the allocation between asset class, sector, currency and holding', async () => {
    setup();
    await flushAll();
    const holding = [...el.querySelectorAll<HTMLButtonElement>('[role=radio]')].find(
      (b) => b.textContent?.trim() === 'Holding',
    )!;
    holding.click();
    fixture.detectChanges();
    expect(holding.getAttribute('aria-checked')).toBe('true');
    const slices = el.querySelector('.slices')!.textContent ?? '';
    expect(slices).toContain('AAPL.US');
    expect(slices).not.toContain('Equity');
  });

  it('explains beta, gross and net exposure and the two returns where they show', async () => {
    setup();
    await flushAll();
    const text = el.textContent ?? '';
    expect(text).toContain('long minus short');
    expect(text).toContain('of holdings covered');
    const explain = el.querySelector('details.explain')!;
    expect(explain.querySelector('summary')?.textContent).toContain('What these figures mean');
    expect(explain.textContent).toContain('Gross exposure');
    expect(explain.textContent).toContain('Net exposure');
    expect(explain.textContent).toContain('Beta');
    // TWR and MWR with one worked example, in the Returns panel.
    const example = el.querySelector('.hint.example')?.textContent ?? '';
    expect(example).toContain('about -1%');
    expect(example).toContain('money-weighted return is worse');
  });

  it('lists what each strategy thinks of each holding, in words', async () => {
    setup();
    await flushAll();
    const agree = el.querySelector('.holdings')!.textContent ?? '';
    expect(agree).toContain('1 agrees, 1 disagrees');
    // Ids without the generated suffix read as they are; generated ones get a name.
    expect(agree).toContain('momentum-v3');
    expect(agree).toContain('agrees.');
    expect(agree).toContain('priced above fair value');
  });

  it('pages the snapshot history and sends the picked portfolio', async () => {
    selected.set('pf_2');
    setup();
    await flushAll();
    expect(el.textContent).toContain('30 snapshots');
    expect(
      seen.some((u) => u.startsWith('/api/insights?') && u.includes('portfolio_id=pf_2')),
    ).toBe(true);
    expect(seen.some((u) => u.includes('/api/portfolio/snapshots') && u.includes('limit=20'))).toBe(
      true,
    );
    // Each snapshot names its run by date, linked, never by its raw id (UX-27).
    const table = el.querySelector('section[aria-labelledby="history-title"] table')!;
    expect(table.textContent).not.toContain('t7');
    // The trading day it is for leads, not the minute the run saved it (m4).
    expect(table.querySelector('a[href="/orders/ticks/t7"]')?.textContent?.trim()).toBe(
      '2026-09-25',
    );
  });

  it('shows totals across every book to admins only', async () => {
    setup();
    await flushAll();
    expect(seen.some((u) => u.startsWith('/api/insights/totals'))).toBe(false);
    expect(el.textContent).not.toContain('All portfolios');
  });

  it('shows admins the totals card with no holdings', async () => {
    admin = true;
    setup();
    await flushAll();
    expect(el.textContent).toContain('All portfolios');
    expect(el.textContent).toContain('$400,000.00');
    expect(el.textContent).toContain('People');
  });

  it('tells admins when totals are held back, not zeros', async () => {
    admin = true;
    totalsBody = { ...TOTALS, total_value: 0, cash: 0, suppressed: true };
    try {
      setup();
      await flushAll();
      expect(el.textContent).toContain('appear once three or more of them trade real money');
      expect(el.textContent).not.toContain('People');
    } finally {
      totalsBody = TOTALS;
    }
  });

  it('keeps other panels when insights fail', async () => {
    setup();
    for (let i = 0; i < 6; i++) {
      http
        .match(() => true)
        .forEach((req) => {
          if (req.request.url.endsWith('/api/insights')) {
            req.flush({ detail: 'boom' }, { status: 500, statusText: 'err' });
          } else respond(req);
        });
      await tick(5);
      fixture.detectChanges();
    }
    expect(el.textContent).toContain('Could not load insights');
    expect(el.textContent).toContain('1 agrees, 1 disagrees');
  });

  it('writes agreement counts in words', () => {
    expect(agreementLine({ ...AGREEMENT.holdings[0], agree: 2, disagree: 0 })).toBe(
      '2 agree, 0 disagree',
    );
  });
});

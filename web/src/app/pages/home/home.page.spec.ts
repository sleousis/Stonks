import { HttpTestingController, provideHttpClientTesting } from '@angular/common/http/testing';
import { TestBed } from '@angular/core/testing';
import { provideRouter } from '@angular/router';

import type { PnlSeries, PortfolioView } from '../../api/models';
import { provideApi } from '../../api/provide-api';
import { SessionService } from '../../core/auth/session.service';
import { ADMIN, TRADER, UNAUTHORIZED, problem } from '../../../testing/auth-fixtures';
import { nextRequest, tick } from '../../../testing/http';
import { book } from '../../../testing/portfolio-fixtures';
import { HomePage } from './home.page';

const PORTFOLIO: PortfolioView = {
  taken_at: '2026-09-25T21:00:00Z',
  tick_id: 't2',
  cash: 25_000,
  positions: [
    {
      ticker: 'AAPL.US',
      quantity: 100,
      price: 230.5,
      price_date: '2026-09-25',
      market_value: 23_050,
      weight: 0.3,
      unrealized_pnl_pct: 0.05,
    },
    {
      ticker: 'MSFT.US',
      quantity: 70,
      price: 410,
      price_date: '2026-09-25',
      market_value: 28_700,
      weight: 0.37,
      unrealized_pnl_pct: -0.02,
    },
  ],
  positions_value: 51_750,
  total_value: 76_750,
  snapshot_total_value: 76_500,
};

const PNL: PnlSeries = {
  strategy_id: null,
  rows: [
    {
      day: '2026-09-25',
      total_value: 76_750,
      daily_change: -1_250,
      daily_return: -0.016,
      cumulative_return: -0.016,
      drawdown: -0.016,
    },
  ],
};

describe('HomePage', () => {
  let controller: HttpTestingController;

  beforeEach(() => {
    TestBed.configureTestingModule({
      providers: [provideRouter([]), ...provideApi(), provideHttpClientTesting()],
    });
    controller = TestBed.inject(HttpTestingController);
  });

  afterEach(() => controller.verify());

  async function signIn(me: typeof TRADER | null) {
    const session = TestBed.inject(SessionService);
    const loading = session.load();
    const req = await nextRequest(controller, '/api/auth/me');
    if (me) {
      req.flush(me);
    } else {
      req.flush({ ...problem(401, 'reads_open'), code: 'reads_open' }, UNAUTHORIZED);
    }
    await loading;
  }

  async function flushCommon() {
    (await nextRequest(controller, '/api/notifications')).flush({ items: [], unread_count: 0 });
    (await nextRequest(controller, '/api/subscriptions')).flush(page([]));
  }

  function page<T>(items: T[]) {
    return { items, total: items.length, limit: 500, offset: 0 };
  }

  it('shows a trader their portfolio, signals and strategies', async () => {
    await signIn(TRADER);
    const fixture = TestBed.createComponent(HomePage);
    fixture.detectChanges();
    (await nextRequest(controller, '/api/portfolios')).flush(
      page([book({ id: 'pf_1', name: 'Main' })]),
    );
    (await nextRequest(controller, '/api/portfolio')).flush(PORTFOLIO);
    (await nextRequest(controller, '/api/pnl')).flush(PNL);
    await flushCommon();
    await tick();
    fixture.detectChanges();
    const el: HTMLElement = fixture.nativeElement;

    expect(el.querySelector('h1')?.textContent).toContain('Hello, Ann');
    const headings = [...el.querySelectorAll('h2')].map((h) => h.textContent?.trim());
    expect(headings).toEqual(['My portfolio', "Today's signals", 'My strategies']);
    expect(el.textContent).toContain('$76,750.00');
    expect(el.textContent).toContain('-$1,250.00');
    // Biggest holding first.
    const tickers = [...el.querySelectorAll('.ticker')].map((t) => t.textContent);
    expect(tickers).toEqual(['MSFT.US', 'AAPL.US']);
    controller.expectNone('/api/portfolio/totals');
  });

  it('shows admins totals across traders instead of holdings', async () => {
    await signIn(ADMIN);
    const fixture = TestBed.createComponent(HomePage);
    fixture.detectChanges();
    (await nextRequest(controller, '/api/portfolio/totals')).flush({
      cash: 1000,
      total_value: 250_000,
      portfolios: 4,
      owners: 3,
    });
    await flushCommon();
    await tick();
    fixture.detectChanges();
    const el: HTMLElement = fixture.nativeElement;
    expect(el.textContent).toContain('All traders');
    expect(el.textContent).toContain('$250,000.00');
    expect(el.textContent).not.toContain('My portfolio');
    controller.expectNone('/api/portfolio');
  });

  it('asks an anonymous dev visitor to sign in', async () => {
    await signIn(null);
    const fixture = TestBed.createComponent(HomePage);
    fixture.detectChanges();
    (await nextRequest(controller, '/api/portfolios')).flush(
      problem(401, 'not_authenticated'),
      UNAUTHORIZED,
    );
    (await nextRequest(controller, '/api/portfolio')).flush(
      problem(401, 'not_authenticated'),
      UNAUTHORIZED,
    );
    (await nextRequest(controller, '/api/pnl')).flush(PNL);
    await flushCommon();
    await tick();
    fixture.detectChanges();
    const el: HTMLElement = fixture.nativeElement;
    expect(el.querySelector('h1')?.textContent).toContain('Today');
    expect(el.querySelector('.banner a')?.getAttribute('href')).toBe('/login');
  });

  it('tells a trader with no portfolio how to get one, and asks for no holdings (BUG-3)', async () => {
    await signIn(TRADER);
    const fixture = TestBed.createComponent(HomePage);
    fixture.detectChanges();
    (await nextRequest(controller, '/api/portfolios')).flush(page([]));
    await flushCommon();
    await tick();
    fixture.detectChanges();
    const el: HTMLElement = fixture.nativeElement;
    expect(el.textContent).toContain('No portfolio yet');
    expect(el.textContent).not.toContain('Could not load');
    controller.expectNone('/api/portfolio');
    controller.expectNone('/api/pnl');
  });
});

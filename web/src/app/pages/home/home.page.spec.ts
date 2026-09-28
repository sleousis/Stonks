import { HttpTestingController, provideHttpClientTesting } from '@angular/common/http/testing';
import { TestBed } from '@angular/core/testing';
import { provideRouter } from '@angular/router';

import type { PnlSeries, PortfolioView } from '../../api/models';
import { provideApi } from '../../api/provide-api';
import { SessionService } from '../../core/auth/session.service';
import { TradingDayService } from '../../core/schedule/trading-day.service';
import { AUTO_REFRESH_MS } from '../../shared/auto-refresh';
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
    TestBed.inject(TradingDayService)['settledSignal'].set(true);
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
    (await nextRequest(controller, '/api/ticks')).flush(page([]));
  }

  /** Signed-in people also get their watchlists and the first-run guide. */
  async function flushPersonal() {
    (await nextRequest(controller, '/api/watchlists')).flush(page([]));
    (await nextRequest(controller, '/api/onboarding')).flush({
      steps: [],
      complete: true,
      dismissed: false,
      show: false,
    });
  }

  async function flushTape() {
    (await nextRequest(controller, '/api/orders/fills')).flush(page([]));
    (await nextRequest(controller, '/api/orders')).flush(page([]));
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
    await flushPersonal();
    await flushTape();
    await tick();
    fixture.detectChanges();
    const el: HTMLElement = fixture.nativeElement;

    expect(el.querySelector('h1')?.textContent).toContain('Hello, Ann');
    expect(el.querySelector('app-fills-tape')?.textContent).toContain('No fills this week');
    const headings = [...el.querySelectorAll('h2')].map((h) => h.textContent?.trim());
    expect(headings).toEqual(['My portfolio', "Today's signals and runs", 'My strategies']);
    expect(el.textContent).toContain('$76,750.00');
    expect(el.textContent).toContain('-$1,250.00');
    // Biggest holding first.
    const tickers = [...el.querySelectorAll('.ticker')].map((t) => t.textContent);
    expect(tickers).toEqual(['MSFT.US', 'AAPL.US']);
    controller.expectNone('/api/portfolio/totals');
  });

  it('shows admins their own portfolio first, with the totals across traders below (M3)', async () => {
    await signIn(ADMIN);
    const fixture = TestBed.createComponent(HomePage);
    fixture.detectChanges();
    (await nextRequest(controller, '/api/portfolios')).flush(
      page([book({ id: 'pf_1', name: 'Main' })]),
    );
    (await nextRequest(controller, '/api/portfolio/totals')).flush({
      cash: 1000,
      total_value: 250_000,
      portfolios: 4,
      owners: 3,
    });
    (await nextRequest(controller, '/api/portfolio')).flush(PORTFOLIO);
    (await nextRequest(controller, '/api/pnl')).flush(PNL);
    await flushCommon();
    await flushPersonal();
    await flushTape();
    await tick();
    fixture.detectChanges();
    const el: HTMLElement = fixture.nativeElement;
    const headings = [...el.querySelectorAll('h2')].map((h) => h.textContent?.trim());
    expect(headings).toEqual([
      'My portfolio',
      'All traders',
      "Today's signals and runs",
      'My strategies',
    ]);
    expect(el.textContent).toContain('$76,750.00');
    expect(el.textContent).toContain('$250,000.00');
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
    await flushTape();
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
    await flushPersonal();
    await tick();
    fixture.detectChanges();
    const el: HTMLElement = fixture.nativeElement;
    expect(el.textContent).toContain('No portfolio yet');
    expect(el.textContent).not.toContain('Could not load');
    controller.expectNone('/api/portfolio');
    controller.expectNone('/api/pnl');
    controller.expectNone('/api/orders/fills');
    expect(el.querySelector('app-fills-tape section')).toBeNull();
  });

  describe('during the session (UX-12)', () => {
    const START = Date.parse('2026-09-28T14:00:00Z');

    beforeEach(() => {
      // Intervals and the clock are fake; setTimeout stays real for the HTTP helpers.
      vi.useFakeTimers({ now: START, toFake: ['setInterval', 'clearInterval', 'Date'] });
    });
    afterEach(() => vi.useRealTimers());

    /** Today with one portfolio, every first read answered. */
    async function openToday() {
      await signIn(TRADER);
      const fixture = TestBed.createComponent(HomePage);
      fixture.detectChanges();
      (await nextRequest(controller, '/api/portfolios')).flush(
        page([book({ id: 'pf_1', name: 'Main' })]),
      );
      (await nextRequest(controller, '/api/portfolio')).flush(PORTFOLIO);
      (await nextRequest(controller, '/api/pnl')).flush(PNL);
      await flushCommon();
      await flushPersonal();
      await flushTape();
      await tick();
      fixture.detectChanges();
      return fixture;
    }

    /** Every request made since the last flush, by path; all answered. */
    async function askedAgain(fixture: { detectChanges(): void }): Promise<string[]> {
      for (let i = 0; i < 5; i++) {
        await tick(2);
        fixture.detectChanges();
      }
      const reqs = controller.match(() => true);
      const paths = reqs.map((r) => r.request.url.split('?')[0]);
      for (const r of reqs) {
        const path = r.request.url.split('?')[0];
        if (path === '/api/portfolio') r.flush(PORTFOLIO);
        else if (path === '/api/pnl') r.flush(PNL);
        else if (path === '/api/notifications') r.flush({ items: [], unread_count: 0 });
        else r.flush(page([]));
      }
      await tick();
      return paths;
    }

    it('after AUTO_REFRESH_MS the feed, fills and portfolio are requested again', async () => {
      const fixture = await openToday();
      expect(await askedAgain(fixture)).toEqual([]);

      vi.advanceTimersByTime(AUTO_REFRESH_MS);
      const paths = await askedAgain(fixture);
      for (const path of [
        '/api/notifications',
        '/api/orders/fills',
        '/api/portfolio',
        '/api/pnl',
        '/api/subscriptions',
      ]) {
        expect(paths).toContain(path);
      }
    });

    it('reloads when the next trading run starts, before the minute is up', async () => {
      const day = TestBed.inject(TradingDayService);
      day['settledSignal'].set(true);
      day['jobsSignal'].set([
        {
          action: 'connections_sync',
          name: 'connections_sync',
          next_run_at: new Date(START + 5_000).toISOString(),
          next_as_of: null,
          trigger: 'interval',
        },
        {
          action: 'tick',
          name: 'tick',
          next_run_at: new Date(START + 20_000).toISOString(),
          next_as_of: null,
          trigger: 'daily',
        },
      ]);
      const fixture = await openToday();
      const el: HTMLElement = fixture.nativeElement;
      expect(el.querySelector('.row.next .title')?.textContent).toContain('Next: Trading run');

      vi.advanceTimersByTime(10_000);
      expect(await askedAgain(fixture)).toEqual([]);

      // The day service counts the run once its time has passed (its own spec covers when).
      day['passed'].update((n) => n + 1);
      const paths = await askedAgain(fixture);
      expect(paths).toContain('/api/notifications');
      expect(paths).toContain('/api/orders/fills');
      expect(paths).toContain('/api/portfolio');
    });
  });
});

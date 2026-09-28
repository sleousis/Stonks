import {
  HttpTestingController,
  type TestRequest,
  provideHttpClientTesting,
} from '@angular/common/http/testing';
import { type ComponentFixture, TestBed } from '@angular/core/testing';
import { provideRouter } from '@angular/router';

import type { HealthReportView, Page, PnlSeries, PortfolioView, TickRun } from '../../api/models';
import { provideApi } from '../../api/provide-api';
import { TicksService } from '../../api/ticks.service';
import { FakeChartEngine, provideFakeChart } from '../../../testing/fake-chart';
import { nextRequest, tick } from '../../../testing/http';
import { DashboardPage } from './dashboard.page';

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
    },
    {
      ticker: 'MSFT.US',
      quantity: 70,
      price: 410,
      price_date: '2026-09-25',
      market_value: 28_700,
      weight: 0.37,
      avg_cost: 400,
      cost_basis: 28_000,
      unrealized_pnl: 700,
      unrealized_pnl_pct: 0.025,
      currency: 'USD',
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
      day: '2026-09-24',
      total_value: 78_000,
      daily_change: null,
      daily_return: null,
      cumulative_return: 0,
      drawdown: 0,
    },
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

const TICKS: Page<TickRun> = {
  items: [
    {
      id: 't2',
      started_at: '2026-09-25T21:00:00Z',
      finished_at: '2026-09-25T21:00:04Z',
      as_of: '2026-09-25',
      status: 'ok',
      summary: {
        orders_placed: 2,
        fills: 2,
        winner_strategy_id: 'momentum-v3',
      } as TickRun['summary'],
    },
    {
      id: 't1',
      started_at: '2026-09-24T21:00:00Z',
      finished_at: '2026-09-24T21:00:01Z',
      status: 'error',
      summary: null,
    },
  ],
  total: 2,
  limit: 8,
  offset: 0,
};

const HEALTH: HealthReportView = {
  healthy: false,
  checked_at: '2026-09-26T08:00:00Z',
  thresholds: {},
  checks: [
    { name: 'freshness:AAPL.US', ok: false, detail: 'latest bar 2026-09-19 (7d old, max 4d)' },
    { name: 'freshness:MSFT.US', ok: true, detail: 'latest bar 2026-09-25 (1d old, max 4d)' },
    { name: 'lab_queue', ok: true, detail: '0 queued, 0 running, 0 worker(s) alive' },
    { name: 'var_violations', ok: true, detail: 'not enough days yet' },
  ],
};

const STRATEGY_COUNTS = { active: 3, shadow: 2, retired: 1, total: 6 };

describe('DashboardPage', () => {
  let fixture: ComponentFixture<DashboardPage>;
  let controller: HttpTestingController;
  let chart: FakeChartEngine;
  let el: HTMLElement;

  beforeEach(async () => {
    chart = new FakeChartEngine();
    TestBed.configureTestingModule({
      imports: [DashboardPage],
      providers: [
        ...provideApi(),
        provideHttpClientTesting(),
        provideRouter([]),
        provideFakeChart(chart),
      ],
    });
    controller = TestBed.inject(HttpTestingController);
    fixture = TestBed.createComponent(DashboardPage);
    el = fixture.nativeElement;
    fixture.detectChanges();
  });

  /**
   * Let responses propagate and re-render. (whenStable() would wait for every
   * resource, including panels a test leaves pending on purpose.)
   */
  async function settle(): Promise<void> {
    for (let i = 0; i < 4; i++) {
      await tick(5);
      fixture.detectChanges();
    }
  }

  afterEach(() => controller.verify());

  /** Answer every pending request with its fixture. */
  function flushPending(): void {
    controller.match(() => true).forEach(respond);
  }

  function respond(req: TestRequest): void {
    {
      const url = new URL(req.request.urlWithParams, 'http://localhost');
      switch (url.pathname) {
        case '/api/portfolio':
          req.flush(PORTFOLIO);
          break;
        case '/api/pnl':
          req.flush(PNL);
          break;
        case '/api/ticks':
          req.flush(TICKS);
          break;
        case '/api/health/report':
          req.flush(HEALTH);
          break;
        case '/api/health':
          req.flush({ status: 'ok', version: '0.1.0' });
          break;
        case '/api/strategies/summary':
          req.flush(STRATEGY_COUNTS);
          break;
        default:
          throw new Error(`unexpected request ${url.pathname}`);
      }
    }
  }

  async function flushAll(): Promise<void> {
    // Requests go out a few microtasks after the component asks for them.
    for (let i = 0; i < 5; i++) {
      flushPending();
      await tick(5);
    }
    await settle();
  }

  it('shows loading placeholders before data arrives', async () => {
    expect(el.querySelectorAll('[role="status"]').length).toBeGreaterThan(0);
    expect(el.textContent).toContain('Loading');
    await flushAll();
  });

  it('renders portfolio figures, positions, ticks, health and strategy counts', async () => {
    await flushAll();
    const text = el.textContent ?? '';

    expect(el.querySelector('h1')?.textContent).toContain('Dashboard');
    expect(text).toContain('$76,750.00');
    expect(text).toContain('$25,000.00');
    expect(text).toContain('32.6% of value');
    // The day's change reads as it does on Today and Insights (M2).
    expect(text).toMatch(/-\$1,250\.00 \(-1\.60%\) (today|on \w+|last session)/);
    // Vocabulary: approved and on trial, never "live" for a paper strategy.
    expect(text).toContain('3 approved');
    expect(text).toContain('2 on trial');
    expect(text).not.toContain('3 live');

    const positionRows = el.querySelectorAll('section[aria-labelledby="positions-title"] tbody tr');
    expect(positionRows.length).toBe(2);
    // Sorted by value, largest first.
    expect(positionRows[0].textContent).toContain('MSFT.US');
    expect(positionRows[0].textContent).toContain('$28,700.00');

    const tickSection = el.querySelector('section[aria-labelledby="ticks-title"]');
    // One status vocabulary: Done and Failed, never ok or error; no raw ids.
    expect(tickSection?.textContent).toContain('Done');
    expect(tickSection?.textContent).toContain('Failed');
    expect(tickSection?.textContent).not.toMatch(/\bok\b|\berror\b/);
    expect(tickSection?.textContent).not.toContain('momentum-v3');
    expect(tickSection?.textContent).toContain('2026-09-25');

    const health = el.querySelector('section[aria-labelledby="health-title"]');
    expect(health?.textContent).toContain('1 of 4 checks failed');
    expect(health?.textContent).toContain('Price data is up to date');
    expect(health?.textContent).toContain('1 of 2 tickers have a recent price.');
    expect(health?.textContent).toContain('Lab workers');
    expect(health?.textContent).toContain('Not enough data yet');
    expect(health?.textContent).not.toMatch(/lab_queue|var_violations|freshness:/);
    expect(health?.textContent).toContain('API 0.1.0');
  });

  it('draws value and drawdown series', async () => {
    await flushAll();
    const series = chart.last;
    expect(series?.map((s) => s.id)).toEqual(['value', 'drawdown']);
    expect(series?.[0].points).toEqual([
      { time: '2026-09-24', value: 78_000 },
      { time: '2026-09-25', value: 76_750 },
    ]);
    expect(series?.[1].pane).toBe(1);
    expect(el.querySelector('[role="img"]')?.getAttribute('aria-label')).toContain('drawdown');
  });

  it('shows the API problem message when a panel fails and retries it', async () => {
    (await nextRequest(controller, '/api/portfolio')).flush(PORTFOLIO);
    (await nextRequest(controller, '/api/pnl')).flush(
      { title: 'Service Unavailable', status: 503, detail: 'state store is locked' },
      { status: 503, statusText: 'Service Unavailable' },
    );
    await settle();

    const alert = el.querySelector('section[aria-labelledby="perf-title"] [role="alert"]');
    expect(alert?.textContent).toContain('state store is locked');

    (alert?.querySelector('button') as HTMLButtonElement).click();
    (await nextRequest(controller, '/api/pnl')).flush(PNL);
    await settle();
    expect(el.querySelector('section[aria-labelledby="perf-title"] [role="alert"]')).toBeNull();

    flushPending();
  });

  it('explains empty states', async () => {
    (await nextRequest(controller, '/api/portfolio')).flush({
      ...PORTFOLIO,
      positions: [],
      positions_value: 0,
    });
    (await nextRequest(controller, '/api/pnl')).flush({ strategy_id: null, rows: [] });
    (await nextRequest(controller, '/api/ticks')).flush({
      items: [],
      total: 0,
      limit: 8,
      offset: 0,
    });
    await settle();

    const text = el.textContent ?? '';
    expect(text).toContain('No open positions');
    expect(text).toContain('No P&L yet');
    expect(text).toContain('No trading runs yet');
    // The tick runner lives on Orders, then Ticks (UI-10).
    const ticks = el.querySelector('section[aria-labelledby="ticks-title"]')!;
    expect(ticks.querySelector('a')!.getAttribute('href')).toBe('/orders/ticks');
    expect(ticks.textContent).not.toContain('`');
    flushPending();
  });

  it('calls /api/strategies/summary once instead of two list calls', async () => {
    const summary = await nextRequest(controller, '/api/strategies/summary');
    expect(controller.match((r) => r.url.split('?')[0] === '/api/strategies').length).toBe(0);
    summary.flush(STRATEGY_COUNTS);
    await flushAll();
    expect(el.textContent).toContain('3 approved');
    expect(el.textContent).toContain('2 on trial');
  });

  it('tick row links to its detail', async () => {
    await flushAll();
    const section = el.querySelector('section[aria-labelledby="ticks-title"]')!;
    const links = [...section.querySelectorAll<HTMLAnchorElement>('tbody a')].map((a) =>
      a.getAttribute('href'),
    );
    expect(links).toContain(`/orders/ticks/${TICKS.items[0].id}`);
    expect(section.querySelector('h2')?.textContent).toBe('Recent trading runs');
  });

  it('health list shows check titles', async () => {
    const pending = await nextRequest(controller, '/api/health/report');
    pending.flush({
      ...HEALTH,
      checks: [
        { name: 'stuck_ticks', ok: true, detail: 'none' },
        { name: 'freshness:AAPL.US', ok: false, detail: '3 days old' },
      ],
    });
    await flushAll();
    const names = [...el.querySelectorAll('.check-name')].map((p) => p.textContent?.trim());
    // Per-ticker freshness folds into one line.
    expect(names).toEqual(['Price data is up to date', 'Stuck trading runs']);
  });

  it('shows when it last updated and reloads when a trading run ends', async () => {
    await flushAll();
    expect(el.querySelector('app-updated-ago')?.textContent).toContain('Updated just now');
    TestBed.inject(TicksService).announceFinished();
    TestBed.tick();
    await tick(5);
    const again = controller.match(() => true);
    const paths = again.map((r) => r.request.url.split('?')[0]).sort();
    expect(paths).toEqual(
      [
        '/api/health',
        '/api/health/report',
        '/api/pnl',
        '/api/portfolio',
        '/api/strategies/summary',
        '/api/ticks',
      ].sort(),
    );
    again.forEach(respond);
    await settle();
  });

  it('shows unrealized P&L with sign and tone, in the portfolio currency', async () => {
    (await nextRequest(controller, '/api/portfolio')).flush({
      ...PORTFOLIO,
      currency: 'EUR',
      unrealized_pnl: 700,
    });
    await settle();
    const row = [
      ...el.querySelectorAll('section[aria-labelledby="positions-title"] tbody tr'),
    ].find((r) => r.textContent?.includes('MSFT.US'))!;
    const pnl = row.querySelector('td[data-label="Unrealized P&L"]')!;
    expect(pnl.textContent?.trim()).toBe('+$700.00');
    expect(pnl.classList).toContain('gain');
    // A summary that fits the panel: cost and P&L % are on Insights.
    expect(row.querySelector('td[data-label="Avg cost"]')).toBeNull();
    expect(row.querySelectorAll('td').length).toBe(5);
    const text = el.textContent ?? '';
    expect(text).toContain('€76,750.00');
    expect(text).toContain('2 positions, +€700.00 unrealized');
    flushPending();
  });
});

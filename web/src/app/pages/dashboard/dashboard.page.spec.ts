import {
  HttpTestingController,
  type TestRequest,
  provideHttpClientTesting,
} from '@angular/common/http/testing';
import { type ComponentFixture, TestBed } from '@angular/core/testing';
import { provideRouter } from '@angular/router';

import type {
  HealthReportView,
  Page,
  PnlSeries,
  PortfolioView,
  StrategySummary,
  TickRun,
} from '../../api/models';
import { provideApi } from '../../api/provide-api';
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
    { name: 'price_freshness', ok: false, detail: 'AAPL.US last bar 3 days old' },
    { name: 'last_tick', ok: true, detail: 'succeeded 11h ago' },
  ],
};

function strategiesPage(total: number): Page<StrategySummary> {
  return { items: [], total, limit: 1, offset: 0 };
}

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
        case '/api/strategies':
          req.flush(strategiesPage(url.searchParams.get('status') === 'active' ? 3 : 2));
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
    expect(text).toContain('-$1,250.00 (-1.60%) on 2026-09-25');
    expect(text).toContain('3 active');
    expect(text).toContain('2 in shadow');

    const positionRows = el.querySelectorAll('section[aria-labelledby="positions-title"] tbody tr');
    expect(positionRows.length).toBe(2);
    // Sorted by value, largest first.
    expect(positionRows[0].textContent).toContain('MSFT.US');
    expect(positionRows[0].textContent).toContain('$28,700.00');

    const tickSection = el.querySelector('section[aria-labelledby="ticks-title"]');
    expect(tickSection?.textContent).toContain('ok');
    expect(tickSection?.textContent).toContain('error');
    expect(tickSection?.textContent).toContain('momentum-v3');

    const health = el.querySelector('section[aria-labelledby="health-title"]');
    expect(health?.textContent).toContain('1 check failing');
    expect(health?.textContent).toContain('AAPL.US last bar 3 days old');
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
    expect(text).toContain('No ticks yet');
    flushPending();
  });
});

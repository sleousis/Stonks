import {
  HttpTestingController,
  type TestRequest,
  provideHttpClientTesting,
} from '@angular/common/http/testing';
import { type ComponentFixture, TestBed } from '@angular/core/testing';
import { provideRouter } from '@angular/router';

import type {
  Page,
  PnlRowView,
  PnlSeries,
  ShadowDecisionView,
  ShadowPnlSummary,
} from '../../api/models';
import { provideApi } from '../../api/provide-api';
import { FakeChartEngine, provideFakeChart } from '../../../testing/fake-chart';
import { tick } from '../../../testing/http';
import { ShadowPage } from './shadow.page';

function row(day: string, total_value: number, drawdown = 0, days_elapsed?: number): PnlRowView {
  return {
    day,
    total_value,
    drawdown,
    days_elapsed,
    daily_change: null,
    daily_return: null,
    cumulative_return: null,
  };
}

const REAL: PnlSeries = {
  strategy_id: null,
  rows: [row('2026-09-01', 200_000), row('2026-09-02', 210_000), row('2026-09-03', 220_000)],
};
const VALUE: PnlSeries = {
  strategy_id: 'value-v1',
  rows: [row('2026-09-02', 100_000, 0, 0), row('2026-09-03', 110_000, 0, 1)],
};

const SUMMARIES: Page<ShadowPnlSummary> = {
  items: [
    {
      strategy_id: 'value-v1',
      days: 2,
      first_day: '2026-09-02',
      latest_day: '2026-09-03',
      cumulative_return: 0.1,
      max_drawdown: 0,
      total_value: 110_000,
      status: 'shadow',
    },
  ],
  total: 1,
  limit: 100,
  offset: 0,
};

const DECISIONS: Page<ShadowDecisionView> = {
  items: [
    {
      id: 1,
      as_of: '2026-09-03',
      created_at: '2026-09-03T21:00:00Z',
      price: 230,
      quantity: 10,
      side: 'buy',
      status: 'filled',
      strategy_id: 'value-v1',
      tick_id: 't3',
      ticker: 'AAPL.US',
    },
  ],
  total: 1,
  limit: 50,
  offset: 0,
};

describe('ShadowPage', () => {
  let fixture: ComponentFixture<ShadowPage>;
  let controller: HttpTestingController;
  let chart: FakeChartEngine;
  let el: HTMLElement;

  beforeEach(() => {
    chart = new FakeChartEngine();
    TestBed.configureTestingModule({
      imports: [ShadowPage],
      providers: [
        ...provideApi(),
        provideHttpClientTesting(),
        provideRouter([]),
        provideFakeChart(chart),
      ],
    });
    controller = TestBed.inject(HttpTestingController);
    fixture = TestBed.createComponent(ShadowPage);
    el = fixture.nativeElement;
    fixture.detectChanges();
  });

  afterEach(() => controller.verify());

  function respond(req: TestRequest): void {
    const path = new URL(req.request.urlWithParams, 'http://localhost').pathname;
    switch (path) {
      case '/api/shadow/pnl':
        return req.flush(SUMMARIES);
      case '/api/pnl':
        return req.flush(REAL);
      case '/api/shadow/strategies/value-v1/pnl':
        return req.flush(VALUE);
      case '/api/shadow/decisions':
        return req.flush(DECISIONS);
      default:
        throw new Error(`unexpected request ${path}`);
    }
  }

  async function flushAll(): Promise<void> {
    for (let i = 0; i < 6; i++) {
      controller.match(() => true).forEach(respond);
      await tick(5);
      fixture.detectChanges();
    }
  }

  it('charts shadow and real rebased to 100 on the same day', async () => {
    await flushAll();
    const series = chart.last;
    expect(series?.map((s) => s.id)).toEqual(['real', 'shadow:value-v1']);
    // Real is rebased on the shadow's first day (210k), not its own first day.
    expect(series?.[0].points).toEqual([
      { time: '2026-09-02', value: 100 },
      { time: '2026-09-03', value: 104.761905 },
    ]);
    expect(series?.[1].points).toEqual([
      { time: '2026-09-02', value: 100 },
      { time: '2026-09-03', value: 110 },
    ]);
    expect(series?.[0].color).toBe('brass');
  });

  it('lists each shadow strategy against the real portfolio with a go-live link', async () => {
    await flushAll();
    const summary = el.querySelector('section[aria-labelledby="summary-title"]');
    const text = summary?.textContent ?? '';
    expect(text).toContain('value-v1');
    expect(text).toContain('+10.00%');
    // 10% vs 4.76% for the real portfolio over the same days.
    expect(text).toContain('+5.24%');
    const goLive = summary?.querySelector<HTMLAnchorElement>('a.go-live');
    expect(goLive?.getAttribute('href')).toBe('/go-live?strategy=value-v1');

    expect(el.textContent).toContain('Leading vs real');
  });

  it('shows shadow decisions with links to their tick', async () => {
    await flushAll();
    const decisions = el.querySelector('section[aria-labelledby="decisions-title"]');
    expect(decisions?.textContent).toContain('AAPL.US');
    expect(decisions?.querySelector('a[href="/orders/ticks/t3"]')).not.toBeNull();
  });

  it('explains the empty state when nothing is in shadow', async () => {
    for (let i = 0; i < 6; i++) {
      controller
        .match(() => true)
        .forEach((req) => {
          const path = new URL(req.request.urlWithParams, 'http://localhost').pathname;
          if (path === '/api/shadow/pnl') req.flush({ items: [], total: 0, limit: 100, offset: 0 });
          else if (path === '/api/shadow/decisions')
            req.flush({ ...DECISIONS, items: [], total: 0 });
          else respond(req);
        });
      await tick(5);
      fixture.detectChanges();
    }
    expect(el.textContent).toContain('Nothing to compare yet');
    expect(el.textContent).toContain('No shadow strategies');
  });
});

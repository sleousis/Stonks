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
import { ShadowPage, aheadOrBehind } from './shadow.page';

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
      strategy_name: null,
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
      strategy_name: null,
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

  it('charts each strategy against your portfolio from its own first day', async () => {
    await flushAll();
    const series = chart.last;
    expect(series?.map((s) => s.id)).toEqual(['real', 'shadow:value-v1']);
    // Your portfolio starts at 100 on the strategy's first day (210k).
    expect(series?.[0].points).toEqual([
      { time: '2026-09-02', value: 100 },
      { time: '2026-09-03', value: 104.761905 },
    ]);
    expect(series?.[1].points).toEqual([
      { time: '2026-09-02', value: 100 },
      { time: '2026-09-03', value: 110 },
    ]);
    // Brass means real money: a paper portfolio's line stays grey.
    expect(series?.[0].color).toBe('muted');
  });

  it('lists each test book against your portfolio with a Review link', async () => {
    await flushAll();
    const summary = el.querySelector('section[aria-labelledby="summary-title"]');
    const text = summary?.textContent ?? '';
    expect(text).toContain('value-v1');
    expect(text).toContain('+10.00%');
    // 10% vs 4.76% for the real portfolio over the same days.
    expect(text).toContain('+5.24%');
    const goLive = summary?.querySelector<HTMLAnchorElement>('a.go-live');
    expect(goLive?.getAttribute('href')).toBe('/strategies/value-v1?tab=review');

    expect(el.textContent).toContain('Best against your portfolio');
    expect(el.textContent).toContain('5.24% ahead of your portfolio');
  });

  it('with a paper portfolio the page never says Real, and names it Your portfolio (UX-26)', async () => {
    await flushAll();
    expect(el.textContent).not.toMatch(/\bReal\b/);
    expect(el.querySelector('.yours')!.textContent).toContain('Your portfolio');
    expect(el.querySelector('.yours app-mode-stamp')!.textContent).toContain('PAPER');
    expect(chart.last?.[0].label).toBe('Your portfolio');
  });

  it('uses trader words only (UX-09)', async () => {
    await flushAll();
    expect(el.querySelector('h1')!.textContent).toBe('Trial results');
    expect(el.textContent).not.toMatch(/shadow|promot|regist|retire/i);
  });

  it('shows strategy names, not ids, and a long name wraps in its tile (UX-27)', async () => {
    const long = 'stocks_on_the_move_breakout_3fa9c21b';
    await flushWith([summary(long, 0.1)]);
    const tile = el.querySelector('app-stat-tile.tile-leader')!;
    expect(tile.textContent).toContain('Stocks on the move breakout 3fa9');
    expect(tile.classList).toContain('text');
    const summaryTable = el.querySelector('section[aria-labelledby="summary-title"]')!;
    expect(summaryTable.textContent).not.toContain(long);
  });

  it('shows paper decisions with links to their run', async () => {
    await flushAll();
    const decisions = el.querySelector('section[aria-labelledby="decisions-title"]');
    expect(decisions?.textContent).toContain('AAPL.US');
    expect(decisions?.querySelector('a[href="/orders/ticks/t3"]')).not.toBeNull();
  });

  function summary(id: string, cumulative_return: number | null): ShadowPnlSummary {
    return { ...SUMMARIES.items[0], strategy_id: id, cumulative_return };
  }

  /** Serve summaries for `ids`; each series answers with VALUE unless it is in `failing`. */
  async function flushWith(items: ShadowPnlSummary[], failing: string[] = []): Promise<string[]> {
    const seriesAsked: string[] = [];
    for (let i = 0; i < 6; i++) {
      controller
        .match(() => true)
        .forEach((req) => {
          const path = new URL(req.request.urlWithParams, 'http://localhost').pathname;
          const m = /^\/api\/shadow\/strategies\/(.+)\/pnl$/.exec(path);
          if (path === '/api/shadow/pnl') {
            req.flush({ items, total: items.length, limit: 100, offset: 0 });
          } else if (m) {
            seriesAsked.push(m[1]);
            if (failing.includes(m[1])) {
              req.flush(
                { title: 'x', status: 500, detail: 'boom' },
                { status: 500, statusText: 'Server Error' },
              );
            } else {
              req.flush({ ...VALUE, strategy_id: m[1] });
            }
          } else {
            respond(req);
          }
        });
      await tick(5);
      fixture.detectChanges();
    }
    return seriesAsked;
  }

  it('one failing series still draws the others', async () => {
    await flushWith([summary('value-v1', 0.1), summary('broken-v0', 0.05)], ['broken-v0']);
    expect(chart.last?.map((s) => s.id)).toEqual(['real', 'shadow:value-v1']);
    const note = el.querySelector('.chart-note.warn');
    expect(note?.textContent).toContain('Could not load broken-v0');
  });

  it('charts only the top strategies by return and says so', async () => {
    const items = Array.from({ length: 9 }, (_, i) => summary(`s${i}`, i / 100));
    const asked = await flushWith(items);
    expect(asked.sort()).toEqual(['s3', 's4', 's5', 's6', 's7', 's8']);
    expect(el.querySelector('.chart-note')?.textContent).toContain('the 6 with the best return');
    // The table still lists all of them.
    const rows = el.querySelectorAll('section[aria-labelledby="summary-title"] tbody tr');
    expect(rows.length).toBe(9);
  });

  it('gives each line a categorical colour and a name, never gain or loss', async () => {
    await flushWith([summary('value-v1', 0.1), summary('quality-v2', 0.05)]);
    const lines = (chart.last ?? []).filter((s) => s.id.startsWith('shadow:'));
    expect(lines.map((s) => s.label)).toEqual(['value-v1', 'quality-v2']);
    for (const s of lines) expect(['gain', 'loss', 'brass']).not.toContain(s.color);
    expect(new Set(lines.map((s) => `${s.color}${s.dashed}`)).size).toBe(2);
    const legend = [...el.querySelectorAll('.legend .item')].map((i) => i.textContent ?? '');
    expect(legend.some((t) => t.includes('quality-v2'))).toBe(true);
  });

  it('shows decision sides as tags and when the page last updated', async () => {
    await flushAll();
    const decisions = el.querySelector('section[aria-labelledby="decisions-title"]')!;
    expect(decisions.querySelector('app-side-tag')?.textContent).toContain('Buy');
    expect(decisions.querySelector('a[href="/orders/ticks/t3"]')?.textContent).toContain(
      'View run',
    );
    expect(el.querySelector('app-updated-ago')?.textContent).toContain('Updated');
  });

  it('explains the empty state when nothing is on trial', async () => {
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
    expect(el.textContent).toContain('No strategies on trial');
  });
});

describe('aheadOrBehind', () => {
  it('never calls a trailing strategy leading (m5)', () => {
    expect(aheadOrBehind(-0.0207)).toBe('2.07% behind your portfolio');
    expect(aheadOrBehind(0.0524)).toBe('5.24% ahead of your portfolio');
    expect(aheadOrBehind(0)).toBe('Level with your portfolio');
    expect(aheadOrBehind(null)).toBeNull();
  });
});

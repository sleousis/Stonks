import { HttpTestingController, provideHttpClientTesting } from '@angular/common/http/testing';
import { TestBed } from '@angular/core/testing';
import { provideRouter } from '@angular/router';

import type { ChartView, CompareView } from '../../api/models';
import { provideApi } from '../../api/provide-api';
import { SessionService } from '../../core/auth/session.service';
import { TRADER } from '../../../testing/auth-fixtures';
import { FakeChartEngine, provideFakeChart } from '../../../testing/fake-chart';
import { nextRequest, page, tick } from '../../../testing/http';
import { book } from '../../../testing/portfolio-fixtures';
import { ChartPage } from './chart.page';

function view(days: number): ChartView {
  const bars = Array.from({ length: days }, (_, i) => {
    const d = new Date(Date.UTC(2025, 0, 1 + i)).toISOString().slice(0, 10);
    const close = 100 + i;
    return {
      timestamp: `${d}T00:00:00`,
      open: close - 0.5,
      high: close + 1,
      low: close - 1,
      close,
      adj_close: close,
      volume: 1_000,
    };
  });
  const last = bars.at(-1)?.timestamp.slice(0, 10) ?? '2025-01-01';
  return {
    ticker: 'UP.US',
    interval: '1d',
    truncated: false,
    portfolio_id: 'pf_1',
    bars,
    fills: [
      {
        filled_at: `${last}T20:00:00Z`,
        side: 'buy',
        quantity: 10,
        price: 120,
        order_client_id: 'o1',
        strategy_id: 'mom',
      },
    ],
    signals: [
      { as_of: last, strategy_id: 'mom', kind: 'entry', strength: 0.5, reason: 'trend is up' },
    ],
  };
}

function comparison(tickers: string[], days = 40, window = 63): CompareView {
  const start = '2025-01-01';
  return {
    start,
    end: '2025-02-09',
    window,
    missing: [],
    series: tickers.map((ticker, k) => {
      const points = Array.from({ length: days }, (_, i) => ({
        time: new Date(Date.UTC(2025, 0, 1 + i)).toISOString().slice(0, 10),
        value: 100 + (k === 0 ? i : -i / 2),
      }));
      return {
        ticker,
        points,
        drawdown: points.map((pt) => ({ time: pt.time, value: k === 0 ? 0 : pt.value / 100 - 1 })),
        rolling_sharpe: points.map((pt) => ({ time: pt.time, value: k === 0 ? 1.5 : -0.5 })),
        total_return: points.at(-1)!.value / 100 - 1,
        max_drawdown: k === 0 ? 0 : points.at(-1)!.value / 100 - 1,
        sharpe: k === 0 ? 1.5 : -0.5,
        periods_per_year: 252,
      };
    }),
  };
}

function param(url: string, name: string): string | null {
  return new URL(url, 'http://test').searchParams.get(name);
}

function limitOf(url: string): string | null {
  return new URL(url, 'http://test').searchParams.get('limit');
}

describe('ChartPage', () => {
  let http: HttpTestingController;
  let engine: FakeChartEngine;

  beforeEach(async () => {
    engine = new FakeChartEngine();
    TestBed.configureTestingModule({
      providers: [
        provideRouter([]),
        ...provideApi(),
        provideHttpClientTesting(),
        provideFakeChart(engine),
      ],
    });
    http = TestBed.inject(HttpTestingController);
    const session = TestBed.inject(SessionService);
    const loading = session.load();
    (await nextRequest(http, '/api/auth/me')).flush(TRADER);
    await loading;
  });

  afterEach(() => http.verify());

  async function render(
    ticker?: string,
    books = [book({ id: 'pf_1', name: 'Main' })],
    vs?: string,
  ) {
    const fixture = TestBed.createComponent(ChartPage);
    if (ticker) fixture.componentRef.setInput('ticker', ticker);
    if (vs) fixture.componentRef.setInput('vs', vs);
    fixture.detectChanges();
    (await nextRequest(http, '/api/portfolios')).flush(page(books));
    (await nextRequest(http, '/api/watchlists')).flush(page([]));
    return fixture;
  }

  it('draws candles, averages, your fills and signals for the ticker', async () => {
    const fixture = await render('up.us');
    const req = await nextRequest(http, '/api/charts/UP.US');
    expect(limitOf(req.request.urlWithParams)).toBe(String(252 + 199));
    req.flush(view(300));
    const cmp = await nextRequest(http, '/api/charts/compare');
    expect(param(cmp.request.urlWithParams, 'tickers')).toBe('UP.US');
    expect(param(cmp.request.urlWithParams, 'limit')).toBe('252');
    cmp.flush(comparison(['UP.US']));
    await tick(5);
    fixture.detectChanges();
    await tick(5);
    fixture.detectChanges();

    const drawn = engine.lastPrice!;
    expect(drawn.candles.length).toBe(252);
    expect(drawn.overlays.map((o) => o.id)).toEqual(['ma50', 'ma200']);
    expect(drawn.markers).toEqual([
      { time: drawn.candles.at(-1)!.time, kind: 'buy', text: 'B' },
      { time: drawn.candles.at(-1)!.time, kind: 'entry', text: '' },
    ]);
    const el: HTMLElement = fixture.nativeElement;
    expect(el.querySelector('h1')?.textContent).toContain('UP.US');
    expect(el.textContent).toContain('trend is up');
    expect(el.querySelector('app-side-tag')).not.toBeNull();

    // Toggles redraw without a new request.
    const fills = [...el.querySelectorAll<HTMLInputElement>('.toggle input')].find((i) =>
      i.parentElement?.textContent?.includes('Fills'),
    )!;
    fills.click();
    fixture.detectChanges();
    await tick(5);
    expect(engine.lastPrice!.markers.map((m) => m.kind)).toEqual(['entry']);

    // A new range asks for more bars.
    const threeYears = [...el.querySelectorAll<HTMLButtonElement>('.seg')].find(
      (b) => b.textContent?.trim() === '3Y',
    )!;
    threeYears.click();
    fixture.detectChanges();
    const wider = await nextRequest(http, '/api/charts/UP.US');
    expect(limitOf(wider.request.urlWithParams)).toBe(String(756 + 199));
    wider.flush(view(300));
    const wideCmp = await nextRequest(http, '/api/charts/compare');
    expect(param(wideCmp.request.urlWithParams, 'limit')).toBe('756');
    wideCmp.flush(comparison(['UP.US']));
    await tick(5);
  });

  it('offers to open a paper portfolio where your fills would be, with no portfolio (UX-13)', async () => {
    const fixture = await render('UP.US', []);
    (await nextRequest(http, '/api/charts/UP.US')).flush(view(300));
    (await nextRequest(http, '/api/charts/compare')).flush(comparison(['UP.US']));
    for (let i = 0; i < 3; i++) {
      await tick(5);
      fixture.detectChanges();
    }
    const el: HTMLElement = fixture.nativeElement;
    const link = el.querySelector<HTMLAnchorElement>('app-no-book a');
    expect(link?.textContent?.trim()).toBe('Open a paper portfolio');
    expect(link?.getAttribute('href')).toBe('/welcome?step=portfolio');
  });

  it('compares tickers on one scale and draws rolling Sharpe and drawdown (13.5)', async () => {
    const fixture = await render('UP.US', undefined, 'msft.us,UP.US');
    (await nextRequest(http, '/api/charts/UP.US')).flush(view(300));
    const cmp = await nextRequest(http, '/api/charts/compare');
    // the chart's own ticker first, itself dropped from ?vs=
    expect(param(cmp.request.urlWithParams, 'tickers')).toBe('UP.US,MSFT.US');
    expect(param(cmp.request.urlWithParams, 'window')).toBe('63');
    cmp.flush(comparison(['UP.US', 'MSFT.US']));
    for (let i = 0; i < 3; i++) {
      await tick(5);
      fixture.detectChanges();
    }
    const el: HTMLElement = fixture.nativeElement;
    const drawn = engine.series.map((s) => s.map((x) => x.id));
    expect(drawn).toContainEqual(['cmp-UP.US', 'cmp-MSFT.US']);
    expect(drawn).toContainEqual(['sharpe', 'drawdown']);
    const rows = [...el.querySelectorAll('.figures-table tbody tr')].map((r) =>
      [...r.children].map((c) => c.textContent?.trim()),
    );
    expect(rows[0]).toEqual(['UP.US', '+39.0%', '0.0%', '1.5']);
    expect(rows[1][0]).toBe('MSFT.US');
    expect(el.querySelector('[aria-label="Stop comparing MSFT.US"]')).not.toBeNull();

    // add a ticker from the box
    const box = el.querySelector<HTMLInputElement>('#compare-ticker')!;
    box.value = 'spy.us';
    box.dispatchEvent(new Event('input'));
    fixture.detectChanges();
    el.querySelector<HTMLFormElement>('.compare-form')!.dispatchEvent(new Event('submit'));
    fixture.detectChanges();
    const more = await nextRequest(http, '/api/charts/compare');
    expect(param(more.request.urlWithParams, 'tickers')).toBe('UP.US,MSFT.US,SPY.US');
    more.flush(comparison(['UP.US', 'MSFT.US', 'SPY.US']));
    await tick(5);
    fixture.detectChanges();

    // remove one, and a longer Sharpe window asks again
    el.querySelector<HTMLButtonElement>('[aria-label="Stop comparing MSFT.US"]')!.click();
    fixture.detectChanges();
    const fewer = await nextRequest(http, '/api/charts/compare');
    expect(param(fewer.request.urlWithParams, 'tickers')).toBe('UP.US,SPY.US');
    fewer.flush(comparison(['UP.US', 'SPY.US']));
    await tick(5);
    fixture.detectChanges();
    const sixMonths = [
      ...el.querySelectorAll<HTMLButtonElement>('[aria-label="Sharpe window"] .seg'),
    ].find((b) => b.textContent?.trim() === '6M')!;
    sixMonths.click();
    fixture.detectChanges();
    const longer = await nextRequest(http, '/api/charts/compare');
    expect(param(longer.request.urlWithParams, 'window')).toBe('126');
    longer.flush(comparison(['UP.US', 'SPY.US'], 40, 126));
    await tick(5);
  });

  it('asks for a ticker when none is open', async () => {
    const fixture = await render();
    await tick();
    fixture.detectChanges();
    const el: HTMLElement = fixture.nativeElement;
    expect(el.textContent).toContain('Pick a ticker');
    http.expectNone((r) => r.url.startsWith('/api/charts'));
  });

  it('says how to load prices when there are none', async () => {
    const fixture = await render('NEW.US');
    (await nextRequest(http, '/api/charts/NEW.US')).flush({
      ...view(0),
      bars: [],
      fills: [],
      signals: [],
      portfolio_id: null,
    });
    (await nextRequest(http, '/api/charts/compare')).flush({
      start: null,
      end: null,
      window: 63,
      series: [],
      missing: ['NEW.US'],
    });
    await tick(5);
    fixture.detectChanges();
    const el: HTMLElement = fixture.nativeElement;
    expect(el.textContent).toContain('No prices yet');
    // A trader cannot update data: no dead end on a page they may not use.
    expect(el.querySelector('a[href="/data"]')).toBeNull();
    expect(el.querySelector('app-empty-state a')?.getAttribute('href')).toBe('/watchlists');
  });
});

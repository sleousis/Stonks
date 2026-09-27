import { HttpTestingController, provideHttpClientTesting } from '@angular/common/http/testing';
import { TestBed } from '@angular/core/testing';
import { provideRouter } from '@angular/router';

import type { ChartView } from '../../api/models';
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

  async function render(ticker?: string) {
    const fixture = TestBed.createComponent(ChartPage);
    if (ticker) fixture.componentRef.setInput('ticker', ticker);
    fixture.detectChanges();
    (await nextRequest(http, '/api/portfolios')).flush(page([book({ id: 'pf_1', name: 'Main' })]));
    (await nextRequest(http, '/api/watchlists')).flush(page([]));
    return fixture;
  }

  it('draws candles, averages, your fills and signals for the ticker', async () => {
    const fixture = await render('up.us');
    const req = await nextRequest(http, '/api/charts/UP.US');
    expect(limitOf(req.request.urlWithParams)).toBe(String(252 + 199));
    req.flush(view(300));
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
    await tick(5);
    fixture.detectChanges();
    const el: HTMLElement = fixture.nativeElement;
    expect(el.textContent).toContain('No prices yet');
  });
});

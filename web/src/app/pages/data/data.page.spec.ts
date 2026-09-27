import { HttpTestingController, provideHttpClientTesting } from '@angular/common/http/testing';
import { type ComponentFixture, TestBed } from '@angular/core/testing';
import { provideRouter } from '@angular/router';

import type { BarSeries, CoverageRow, IngestRunView, InstrumentView, Page } from '../../api/models';
import { provideApi } from '../../api/provide-api';
import { FakeChartEngine, provideFakeChart } from '../../../testing/fake-chart';
import { nextRequest, tick } from '../../../testing/http';
import { DataPage } from './data.page';

function page<T>(items: T[]): Page<T> {
  return { items, total: items.length, limit: 25, offset: 0 };
}

const daysAgo = (n: number) => new Date(Date.now() - n * 86_400_000).toISOString();

const INSTRUMENTS = page<InstrumentView>([
  {
    id: 'AAPL.US',
    name: 'Apple Inc',
    asset_class: 'equity',
    exchange: 'US',
    currency: 'USD',
    sector: null,
    industry: null,
    is_delisted: false,
  },
]);

const COVERAGE = page<CoverageRow>([
  { ticker: 'AAPL.US', interval: '1d', first_bar: daysAgo(400), last_bar: daysAgo(1), rows: 280 },
  { ticker: 'MSFT.US', interval: '1d', first_bar: daysAgo(400), last_bar: daysAgo(40), rows: 250 },
]);

const RUNS = page<IngestRunView>([
  {
    id: 3,
    source: 'eodhd',
    kind: 'prices',
    started_at: daysAgo(1),
    finished_at: daysAgo(1),
    tickers_ok: 1,
    tickers_failed: 1,
    status: 'partial',
    error: 'MSFT.US: HTTP 404',
  },
]);

const BARS: BarSeries = {
  ticker: 'AAPL.US',
  interval: '1d',
  truncated: false,
  bars: [
    {
      timestamp: '2026-09-24T00:00:00Z',
      open: 1,
      high: 1,
      low: 1,
      close: 100,
      adj_close: 100,
      volume: 10,
    },
    {
      timestamp: '2026-09-25T00:00:00Z',
      open: 1,
      high: 1,
      low: 1,
      close: 110,
      adj_close: 110,
      volume: 12,
    },
  ],
};

describe('DataPage', () => {
  let fixture: ComponentFixture<DataPage>;
  let http: HttpTestingController;
  let chart: FakeChartEngine;
  let el: HTMLElement;

  async function settle(): Promise<void> {
    await tick();
    fixture.detectChanges();
    await tick();
    fixture.detectChanges();
  }

  beforeEach(async () => {
    chart = new FakeChartEngine();
    TestBed.configureTestingModule({
      providers: [
        ...provideApi(),
        provideHttpClientTesting(),
        provideFakeChart(chart),
        provideRouter([]),
      ],
    });
    http = TestBed.inject(HttpTestingController);
    fixture = TestBed.createComponent(DataPage);
    el = fixture.nativeElement;
    fixture.detectChanges();

    (await nextRequest(http, '/api/catalog/intervals')).flush([
      { code: '1d', is_intraday: false, seconds: 86_400 },
      { code: '5m', is_intraday: true, seconds: 300 },
    ]);
    (await nextRequest(http, '/api/market/instruments')).flush(INSTRUMENTS);
    (await nextRequest(http, '/api/market/coverage')).flush(COVERAGE);
    (await nextRequest(http, '/api/sources')).flush([
      { id: 'eodhd', configured: true, default: true, detail: null },
    ]);
    (await nextRequest(http, '/api/ingest/runs')).flush(RUNS);
    await settle();
  });

  it('links to the stored universes', () => {
    expect(el.querySelector('a[href="/universes"]')?.textContent).toContain('Universes');
  });

  it('shows coverage with a freshness pill per series', () => {
    const coverage = el.querySelector('[aria-labelledby="coverage-title"]')!;
    expect(coverage.textContent).toContain('AAPL.US');
    expect(coverage.textContent).toContain('Fresh');
    expect(coverage.textContent).toContain('Old');
  });

  it('shows data updates with status and error text in trader words', () => {
    const runs = el.querySelector('[aria-labelledby="runs-title"]')!;
    expect(runs.textContent).toContain('Partly done');
    expect(runs.textContent).toContain('Daily prices');
    expect(runs.textContent).toContain('EODHD');
    expect(runs.textContent).not.toMatch(/\b(partial|eodhd)\b/);
    expect(runs.textContent).toContain('MSFT.US: HTTP 404');
  });

  it('ingest is absent from the Data page', () => {
    expect(el.textContent).not.toMatch(/ingest/i);
    expect(el.textContent).not.toContain('Run #');
    expect(el.textContent).toContain('Update data');
    expect(el.textContent).toContain('Data updates');
  });

  it('charts a ticker picked from the search and narrows coverage to it', async () => {
    const pick = el.querySelector<HTMLButtonElement>('app-instrument-search button.pick')!;
    pick.click();
    fixture.detectChanges();

    const coverage = await nextRequest(http, '/api/market/coverage');
    expect(coverage.request.urlWithParams).toContain('ticker=AAPL.US');
    coverage.flush(page([COVERAGE.items[0]]));
    const bars = await nextRequest(http, '/api/market/bars');
    expect(bars.request.urlWithParams).toContain('ticker=AAPL.US');
    expect(bars.request.urlWithParams).toContain('interval=1d');
    bars.flush(BARS);
    await settle();
    await tick(10);

    expect(el.querySelector('[aria-labelledby="price-title"]')!.textContent).toContain('+10.00%');
    expect(chart.last?.[0].points).toEqual([
      { time: '2026-09-24', value: 100 },
      { time: '2026-09-25', value: 110 },
    ]);
  });

  it('searches instruments by text after a pause', async () => {
    const input = el.querySelector<HTMLInputElement>('#instrument-q')!;
    input.value = 'apple';
    input.dispatchEvent(new Event('input'));
    await tick(300);
    fixture.detectChanges();
    const req = await nextRequest(http, '/api/market/instruments');
    expect(req.request.urlWithParams).toContain('q=apple');
    req.flush(INSTRUMENTS);
  });
});

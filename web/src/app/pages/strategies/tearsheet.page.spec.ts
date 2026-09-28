import { HttpTestingController, provideHttpClientTesting } from '@angular/common/http/testing';
import { TestBed } from '@angular/core/testing';
import { provideRouter } from '@angular/router';

import type { TearSheetView } from '../../api/models';
import { provideApi } from '../../api/provide-api';
import { FakeChartEngine, provideFakeChart } from '../../../testing/fake-chart';
import { nextRequest, tick } from '../../../testing/http';
import { STRATEGY_METADATA, paper } from '../../../testing/strategy-fixtures';
import { PrintService } from '../../shared/print.service';
import { curveSeries, yearRows } from './tearsheet-data';
import { TearsheetPage } from './tearsheet.page';

const CURVE = [
  {
    day: '2026-08-31',
    total_value: 10_000,
    daily_change: null,
    daily_return: null,
    cumulative_return: 0,
    drawdown: 0,
  },
  {
    day: '2026-09-25',
    total_value: 10_300,
    daily_change: 300,
    daily_return: 0.03,
    cumulative_return: 0.03,
    drawdown: -0.01,
  },
];

const SHEET: TearSheetView = {
  strategy: {
    id: 'mom_v2',
    class_path: 'stonks.strategies.examples.momentum:Momentum',
    status: 'shadow',
    params: {},
    applicable_asset_classes: ['equity'],
    metadata: STRATEGY_METADATA,
    created_at: '2026-08-01T00:00:00Z',
    updated_at: '2026-08-01T00:00:00Z',
    survival_reports: [
      { test_id: 'oos', passed: true, metrics: { sharpe_oos: 1.2 }, notes: '' },
      { test_id: 'pbo', passed: false, metrics: { pbo: 0.6 }, notes: '' },
    ],
    status_history: [],
  },
  paper: paper({ days: 2, trades: 1 }),
  curve: CURVE,
  monthly_returns: [
    { month: '2025-12', value: -0.02 },
    { month: '2026-08', value: 0.01 },
    { month: '2026-09', value: 0.03 },
  ],
  recent_trades: [
    {
      id: 1,
      tick_id: 't1',
      strategy_id: 'mom_v2',
      as_of: '2026-09-25',
      ticker: 'UP.US',
      side: 'buy',
      quantity: 10,
      price: 100,
      status: 'filled',
      created_at: '2026-09-25T21:00:00Z',
    },
  ],
  book_trades: 3,
  live_since: null,
  golive: {
    strategy_id: 'mom_v2',
    status: 'shadow',
    source: 'shadow',
    passed: false,
    checks: [{ name: 'min_days', passed: false, value: 2, limit: 20, detail: '2 of 20 days' }],
    policy: {},
  },
  status_history: [
    {
      id: 1,
      kind: 'status',
      from_status: null,
      to_status: 'shadow',
      actor: 'user:ann',
      reason: 'first paper run',
      override: false,
      golive_passed: null,
      golive_report: null,
      created_at: '2026-08-01T00:00:00Z',
    },
  ],
};

describe('tear sheet data', () => {
  it('lays monthly returns out by year, newest first, with the year compounded', () => {
    const rows = yearRows(SHEET.monthly_returns);
    expect(rows.map((r) => r.year)).toEqual(['2026', '2025']);
    expect(rows[0].cells[8].text).toBe('+3.0%');
    expect(rows[0].cells[0].text).toBe('');
    expect(rows[0].total).toBe('+4.0%');
    expect(rows[1].cells[11].tone).toBe('loss');
  });

  it('draws the test book value in cool blue, never brass', () => {
    const [value, dd] = curveSeries(CURVE);
    expect(value.color).toBe('primary');
    expect(dd).toMatchObject({ kind: 'area', pane: 1, color: 'loss' });
  });
});

describe('TearsheetPage', () => {
  let http: HttpTestingController;
  let engine: FakeChartEngine;

  beforeEach(() => {
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
  });

  afterEach(() => http.verify());

  it('gathers the verdict, trial result, tests, trades and history', async () => {
    const fixture = TestBed.createComponent(TearsheetPage);
    fixture.componentRef.setInput('id', 'mom_v2');
    fixture.detectChanges();
    (await nextRequest(http, '/api/strategies/mom_v2/tearsheet')).flush(SHEET);
    await tick(5);
    fixture.detectChanges();
    await tick(5);
    const el: HTMLElement = fixture.nativeElement;
    expect(el.querySelector('h1')?.textContent).toContain('mom_v2');
    expect(el.textContent).toContain('Momentum. On trial.');
    // Only trial days are short: promising, not yet judged (F33).
    expect(el.querySelector('app-strategy-verdict')?.textContent).toContain(
      'Promising, needs more data',
    );
    expect(el.textContent).toContain('Sharpe');
    expect(el.textContent).toContain('Failed');
    expect(el.textContent).toContain('first paper run');
    expect(el.querySelectorAll('.months tbody tr').length).toBe(2);
    expect(el.querySelector('app-side-tag')).not.toBeNull();
    expect(engine.last?.map((s) => s.id)).toEqual(['value', 'drawdown']);
    const backtest = [...el.querySelectorAll('a')].find((a) => a.textContent?.includes('Backtest'));
    expect(backtest?.getAttribute('href')).toBe('/lab?strategy=mom_v2');
  });

  it('prints the tear sheet through the browser for a PDF', async () => {
    const print = vi.spyOn(TestBed.inject(PrintService), 'print').mockResolvedValue(undefined);
    const fixture = TestBed.createComponent(TearsheetPage);
    fixture.componentRef.setInput('id', 'mom_v2');
    fixture.detectChanges();
    const el: HTMLElement = fixture.nativeElement;
    const button = [...el.querySelectorAll<HTMLButtonElement>('button')].find((b) =>
      b.textContent?.includes('Download PDF'),
    )!;
    expect(button.disabled).toBe(true); // nothing to print before it loads
    (await nextRequest(http, '/api/strategies/mom_v2/tearsheet')).flush(SHEET);
    await tick(5);
    fixture.detectChanges();
    expect(button.disabled).toBe(false);
    button.click();
    expect(print).toHaveBeenCalledTimes(1);
    expect(el.querySelector('.print-only')?.textContent).toContain('Trial results on paper money');
  });
});

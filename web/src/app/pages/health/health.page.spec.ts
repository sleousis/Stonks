import { HttpTestingController, provideHttpClientTesting } from '@angular/common/http/testing';
import { type ComponentFixture, TestBed } from '@angular/core/testing';

import type { HealthReportView, IngestRunView, Page, TickRun } from '../../api/models';
import { provideApi } from '../../api/provide-api';
import { nextRequest, tick } from '../../../testing/http';
import { HealthPage } from './health.page';

const REPORT: HealthReportView = {
  checked_at: '2026-09-26T12:00:00Z',
  healthy: false,
  checks: [
    { name: 'freshness:AAPL.US', ok: true, detail: 'latest bar 2026-09-25 (1d old, max 4d)' },
    { name: 'freshness:MSFT.US', ok: false, detail: 'latest bar 2026-09-19 (7d old, max 4d)' },
    { name: 'stuck_ticks', ok: true, detail: 'none' },
    { name: 'stuck_ingest_runs', ok: true, detail: 'none' },
    { name: 'ingest_failures', ok: false, detail: 'failed in last 24h: #7 (prices)' },
  ],
};

const FAILED_INGEST: Page<IngestRunView> = {
  items: [
    {
      id: 7,
      source: 'eodhd',
      kind: 'prices',
      started_at: '2026-09-26T06:00:00Z',
      finished_at: '2026-09-26T06:00:05Z',
      tickers_ok: 0,
      tickers_failed: 2,
      status: 'error',
      error: 'HTTP 402 payment required',
    },
  ],
  total: 1,
  limit: 10,
  offset: 0,
};

const NO_TICKS: Page<TickRun> = { items: [], total: 0, limit: 10, offset: 0 };

describe('HealthPage', () => {
  let fixture: ComponentFixture<HealthPage>;
  let http: HttpTestingController;
  let el: HTMLElement;

  async function flushAll(report: HealthReportView = REPORT): Promise<void> {
    (await nextRequest(http, '/api/health/report')).flush(report);
    (await nextRequest(http, '/api/health')).flush({ status: 'ok', version: '1.2.3' });
    const ingest = await nextRequest(http, '/api/ingest/runs');
    expect(ingest.request.urlWithParams).toContain('status=error');
    ingest.flush(FAILED_INGEST);
    const ticks = await nextRequest(http, '/api/ticks');
    expect(ticks.request.urlWithParams).toContain('status=failed');
    ticks.flush(NO_TICKS);
    await tick();
    fixture.detectChanges();
  }

  beforeEach(() => {
    TestBed.configureTestingModule({ providers: [...provideApi(), provideHttpClientTesting()] });
    http = TestBed.inject(HttpTestingController);
    fixture = TestBed.createComponent(HealthPage);
    el = fixture.nativeElement;
    fixture.detectChanges();
  });

  it('shows the overall warning state with counts', async () => {
    await flushAll();
    const overall = el.querySelector('.overall')!;
    expect(overall.getAttribute('data-level')).toBe('warning');
    expect(overall.textContent).toContain('Warning');
    expect(overall.textContent).toContain('2 warnings');
    expect(overall.textContent).toContain('API 1.2.3');
  });

  it('lists freshness per ticker, worst first', async () => {
    await flushAll();
    const text = el.querySelector('[aria-labelledby="fresh-title"]')!.textContent!;
    expect(text.indexOf('MSFT.US')).toBeLessThan(text.indexOf('AAPL.US'));
    expect(text).toContain('7d old');
  });

  it('shows recent ingest failures with their error text', async () => {
    await flushAll();
    expect(el.textContent).toContain('HTTP 402 payment required');
    expect(el.textContent).toContain('No failed ticks');
  });

  it('is critical when a run is stuck', async () => {
    await flushAll({
      ...REPORT,
      checks: [{ name: 'stuck_ticks', ok: false, detail: 'running > 30m: t9' }],
    });
    expect(el.querySelector('.overall')!.getAttribute('data-level')).toBe('critical');
    expect(el.textContent).toContain('running > 30m: t9');
  });

  it('is good when every check passes', async () => {
    await flushAll({ ...REPORT, healthy: true, checks: [{ name: 'stuck_ticks', ok: true, detail: 'none' }] });
    expect(el.querySelector('.overall')!.getAttribute('data-level')).toBe('good');
    expect(el.textContent).toContain('The check passes');
  });

  it('checks chosen tickers', async () => {
    await flushAll();
    const input = el.querySelector<HTMLInputElement>('#health-tickers')!;
    input.value = 'nvda.us, tsla.us';
    el.querySelector<HTMLFormElement>('.ticker-form')!.dispatchEvent(new Event('submit'));
    fixture.detectChanges();
    const req = await nextRequest(http, '/api/health/report');
    expect(req.request.urlWithParams).toContain('tickers=NVDA.US');
    expect(req.request.urlWithParams).toContain('tickers=TSLA.US');
    req.flush(REPORT);
  });

  it('refreshes every panel', async () => {
    await flushAll();
    const refresh = [...el.querySelectorAll('button')].find((b) => b.textContent?.trim() === 'Refresh')!;
    refresh.click();
    await flushAll();
    http.verify();
  });
});

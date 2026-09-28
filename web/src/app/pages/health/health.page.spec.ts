import { HttpTestingController, provideHttpClientTesting } from '@angular/common/http/testing';
import { provideRouter } from '@angular/router';
import { type ComponentFixture, TestBed } from '@angular/core/testing';

import type { HealthReportView, IngestRunView, Page, TickRun } from '../../api/models';
import { provideApi } from '../../api/provide-api';
import { SessionService } from '../../core/auth/session.service';
import { ConfirmService } from '../../core/confirm/confirm.service';
import { HaltStateService } from '../../core/halts/halt-state.service';
import { ToastService } from '../../core/notify/toast.service';
import { ADMIN } from '../../../testing/auth-fixtures';
import { nextRequest, tick } from '../../../testing/http';
import { HealthPage } from './health.page';

const REPORT: HealthReportView = {
  checked_at: '2026-09-26T12:00:00Z',
  thresholds: {
    max_bar_age_days: 4,
    stuck_tick_minutes: 30,
    stuck_ingest_minutes: 120,
    ingest_failure_lookback_hours: 24,
  },
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
const FAILED_TICKS: Page<TickRun> = {
  items: [
    {
      id: '20260926-t9',
      started_at: '2026-09-26T20:45:00Z',
      finished_at: '2026-09-26T20:45:30Z',
      status: 'error',
      summary: { error: 'broker down', error_type: 'BrokerError' },
    } as TickRun,
  ],
  total: 1,
  limit: 10,
  offset: 0,
};

describe('HealthPage', () => {
  let fixture: ComponentFixture<HealthPage>;
  let http: HttpTestingController;
  let el: HTMLElement;

  async function flushAll(
    report: HealthReportView = REPORT,
    ticksPage: Page<TickRun> = NO_TICKS,
  ): Promise<void> {
    (await nextRequest(http, '/api/health/report')).flush(report);
    (await nextRequest(http, '/api/health')).flush({ status: 'ok', version: '1.2.3' });
    const ingest = await nextRequest(http, '/api/ingest/runs');
    expect(ingest.request.urlWithParams).toContain('status=error');
    ingest.flush(FAILED_INGEST);
    const ticks = await nextRequest(http, '/api/ticks');
    expect(ticks.request.urlWithParams).toContain('status=error');
    ticks.flush(ticksPage);
    // The system alerts panel loads on its own, once.
    http
      .match((r) => r.url.split('?')[0] === '/api/alerts')
      .forEach((r) => r.flush({ items: [], total: 0, limit: 20, offset: 0 }));
    // So does the broker gateways panel (its own spec covers it).
    http
      .match((r) => r.url.split('?')[0] === '/api/brokers/gateways')
      .forEach((r) => r.flush({ configured: false, gateways: [] }));
    // And the reconcile panel (its own spec covers it).
    http.match((r) => r.url.split('?')[0] === '/api/reconcile/reports').forEach((r) => r.flush([]));
    // And the price check panel (its own spec covers it).
    http
      .match((r) => r.url.split('?')[0] === '/api/health/price-check')
      .forEach((r) => r.flush(null));
    await tick();
    fixture.detectChanges();
  }

  beforeEach(() => {
    TestBed.configureTestingModule({
      providers: [provideRouter([]), ...provideApi(), provideHttpClientTesting()],
    });
    http = TestBed.inject(HttpTestingController);
    fixture = TestBed.createComponent(HealthPage);
    el = fixture.nativeElement;
    fixture.detectChanges();
  });

  it('shows the overall warning state with counts', async () => {
    await flushAll();
    const overall = el.querySelector('.overall')!;
    expect(overall.getAttribute('data-level')).toBe('warning');
    expect(overall.textContent).toContain('2 of 5 checks failed');
    expect(overall.textContent).toContain('Needs a look soon');
    expect(overall.textContent).toContain('API 1.2.3');
  });

  it('shows the price check panel once', async () => {
    await flushAll();
    expect(el.querySelectorAll('app-price-check-panel').length).toBe(1);
  });

  it('lists freshness per ticker, worst first', async () => {
    await flushAll();
    const text = el.querySelector('[aria-labelledby="fresh-title"]')!.textContent!;
    expect(text.indexOf('MSFT.US')).toBeLessThan(text.indexOf('AAPL.US'));
    expect(text).toContain('Latest price 2026-09-19, 7 days old.');
    expect(text).not.toContain('Good');
  });

  it('names every system check in words with what it watches', async () => {
    await flushAll({
      ...REPORT,
      checks: [
        ...REPORT.checks,
        { name: 'lab_queue', ok: true, detail: '0 queued, 0 running, 0 worker(s) alive' },
        { name: 'risk_halts', ok: true, detail: 'no halt in force' },
        { name: 'var_violations', ok: true, detail: 'not enough days yet' },
      ],
    });
    const text = el.querySelector('[aria-labelledby="runs-title"]')!.textContent!;
    expect(text).toContain('System checks');
    expect(text).toContain('Lab workers');
    expect(text).toContain('Trading stops');
    expect(text).toContain('Risk estimate accuracy');
    expect(text).toContain('Not enough data yet');
    expect(text).toContain('0 waiting, 0 running, 0 workers up.');
    expect(text).toContain('Lab jobs sent to separate workers');
    expect(text).not.toMatch(/lab_queue|risk_halts|var_violations|worker\(s\)/);
    expect(text).not.toContain('Good');
    // Failing checks come first.
    expect(text.indexOf('Recent data update failures')).toBeLessThan(text.indexOf('Lab workers'));
    // Actions are for admins; this reader is not one.
    expect(el.querySelector('.check-action')).toBeNull();
  });

  it('shows recent data update failures with their error text in trader words', async () => {
    await flushAll();
    const section = el.querySelector('[aria-labelledby="ingest-fail-title"]')!;
    expect(section.textContent).toContain('HTTP 402 payment required');
    expect(section.textContent).toContain('Daily prices');
    expect(el.textContent).toContain('No failed trading runs');
  });

  it('links each failed trading run to its page', async () => {
    await flushAll(REPORT, FAILED_TICKS);
    const section = el.querySelector('[aria-labelledby="tick-fail-title"]')!;
    const link = section.querySelector('a');
    expect(link?.getAttribute('href')).toBe('/orders/ticks/20260926-t9');
    expect(section.textContent).toContain('broker down');
  });

  it('says when it last updated', async () => {
    await flushAll();
    expect(el.querySelector('app-updated-ago')?.textContent).toContain('Updated just now');
  });

  it('Refresh reloads the alerts too', async () => {
    await flushAll();
    [...el.querySelectorAll('button')].find((b) => b.textContent?.trim() === 'Refresh')!.click();
    fixture.detectChanges();
    (await nextRequest(http, '/api/alerts')).flush({ items: [], total: 0, limit: 20, offset: 0 });
    await flushAll();
    http.verify();
  });

  it('is critical when a run is stuck', async () => {
    await flushAll({
      ...REPORT,
      checks: [{ name: 'stuck_ticks', ok: false, detail: 'running > 30m: t9' }],
    });
    expect(el.querySelector('.overall')!.getAttribute('data-level')).toBe('critical');
    expect(el.textContent).toContain('1 trading run running over 30 minutes.');
    expect(el.textContent).toContain('Act now');
    expect(el.querySelector('.check-limit')?.textContent).toContain(
      'Stuck when running over 30 min',
    );
  });

  it('is good when every check passes', async () => {
    await flushAll({
      ...REPORT,
      healthy: true,
      checks: [{ name: 'stuck_ticks', ok: true, detail: 'none' }],
    });
    expect(el.querySelector('.overall')!.getAttribute('data-level')).toBe('good');
    expect(el.textContent).toContain('The check passed');
  });

  it('shows the recent system alerts panel (UI-07)', async () => {
    await flushAll();
    expect(el.querySelector('app-alerts-panel')).not.toBeNull();
  });

  it('shows the broker gateways panel, not their checks among the runs', async () => {
    await flushAll({
      ...REPORT,
      checks: [
        { name: 'stuck_ticks', ok: true, detail: 'none' },
        { name: 'broker:live', ok: false, detail: 'live gateway down since 14:05' },
      ],
    });
    expect(el.querySelector('app-gateway-panel')).not.toBeNull();
    expect(el.querySelector('.checks')?.textContent).not.toContain('broker:live');
    expect(el.querySelector('.overall')!.getAttribute('data-level')).toBe('critical');
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
    const refresh = [...el.querySelectorAll('button')].find(
      (b) => b.textContent?.trim() === 'Refresh',
    )!;
    refresh.click();
    await flushAll();
    http.verify();
  });

  it('explains a missing trading universe in plain words', async () => {
    await flushAll({
      ...REPORT,
      checks: REPORT.checks.filter((c) => !c.name.startsWith('freshness:')),
    });
    const text = el.querySelector('[aria-labelledby="fresh-title"]')!.textContent!;
    expect(text).toContain('No universe is set for trading yet. An admin chooses');
    expect(text).not.toContain('Ask your admin');
    expect(el.textContent).not.toMatch(/\[[a-z_.]+\]/);
    expect(el.textContent).not.toContain('ingest');
  });

  it('keeps Run checks now for admins', async () => {
    await flushAll();
    const run = [...el.querySelectorAll('button')].find((b) =>
      b.textContent?.includes('Run checks now'),
    )!;
    expect(run.disabled).toBe(true);
  });
});

describe('HealthPage run checks now (admin)', () => {
  it('confirms, runs the checks, then reloads the report and the halt state', async () => {
    const confirm = vi.fn().mockResolvedValue(true);
    const refresh = vi.fn().mockResolvedValue(undefined);
    TestBed.configureTestingModule({
      providers: [
        provideRouter([]),
        ...provideApi(),
        provideHttpClientTesting(),
        { provide: ConfirmService, useValue: { confirm } },
        { provide: HaltStateService, useValue: { refresh } },
      ],
    });
    const http = TestBed.inject(HttpTestingController);
    const signingIn = TestBed.inject(SessionService).load();
    (await nextRequest(http, '/api/auth/me')).flush(ADMIN);
    await signingIn;
    const success = vi.spyOn(TestBed.inject(ToastService), 'success');
    const fixture = TestBed.createComponent(HealthPage);
    const el: HTMLElement = fixture.nativeElement;
    fixture.detectChanges();
    (await nextRequest(http, '/api/health/report')).flush(REPORT);
    await tick();
    fixture.detectChanges();

    const run = [...el.querySelectorAll('button')].find((b) =>
      b.textContent?.includes('Run checks now'),
    )!;
    expect(run.disabled).toBe(false);
    run.click();
    const post = await nextRequest(http, '/api/health/run', 'POST');
    expect(confirm).toHaveBeenCalledWith(
      expect.objectContaining({
        confirmLabel: 'Run checks now',
        tone: 'danger',
        message: expect.stringContaining('stops trading for everyone'),
      }),
    );
    expect(post.request.body).toEqual({ tickers: null });
    post.flush({ ...REPORT, healthy: true });
    await tick();
    (await nextRequest(http, '/api/health/report')).flush({ ...REPORT, healthy: true });
    await tick();
    expect(refresh).toHaveBeenCalled();
    expect(success).toHaveBeenCalledWith('Ran the health checks: all pass.');
  });

  it('gives an admin the actual fix, never "ask your admin"', async () => {
    TestBed.configureTestingModule({
      providers: [provideRouter([]), ...provideApi(), provideHttpClientTesting()],
    });
    const http = TestBed.inject(HttpTestingController);
    const signingIn = TestBed.inject(SessionService).load();
    (await nextRequest(http, '/api/auth/me')).flush(ADMIN);
    await signingIn;
    const fixture = TestBed.createComponent(HealthPage);
    const el: HTMLElement = fixture.nativeElement;
    fixture.detectChanges();
    (await nextRequest(http, '/api/health/report')).flush({
      ...REPORT,
      checks: [
        { name: 'stuck_ticks', ok: false, detail: 'running > 30m: t1' },
        { name: 'risk_halts', ok: false, detail: '#3 kill_switch (global, all)' },
      ],
    });
    await tick();
    fixture.detectChanges();
    const fresh = el.querySelector('[aria-labelledby="fresh-title"]')!;
    expect(fresh.textContent).toContain('choose it as the trading universe in Settings');
    expect(el.textContent).not.toContain('Ask your admin');
    const links = [...el.querySelectorAll<HTMLAnchorElement>('.check-action')].map((a) => [
      a.textContent?.trim(),
      a.getAttribute('href'),
    ]);
    expect(links).toEqual([
      ['Open trading runs', '/orders/ticks'],
      ['Open halts', '/ops/halts'],
    ]);
    expect(el.textContent).toContain('1 trading run running over 30 minutes.');
    expect(el.textContent).toContain('1 stop in force.');
  });
});

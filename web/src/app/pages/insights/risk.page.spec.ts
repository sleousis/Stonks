import {
  HttpTestingController,
  type TestRequest,
  provideHttpClientTesting,
} from '@angular/common/http/testing';
import { Component, signal } from '@angular/core';
import { type ComponentFixture, TestBed } from '@angular/core/testing';
import { provideRouter } from '@angular/router';

import { provideApi } from '../../api/provide-api';
import { PortfolioContextService } from '../../core/portfolio/portfolio-context.service';
import { FakeChartEngine, provideFakeChart } from '../../../testing/fake-chart';
import { tick } from '../../../testing/http';
import { INSIGHTS, LIVE, POLICY, RISK_HISTORY } from './insights.fixtures';
import { WhyNotPanel } from '../../shared/ui/why-not-panel';
import { RiskPage, violationText } from './risk.page';

/** The why-not panel has its own spec. */
@Component({ selector: 'app-why-not-panel', template: 'Why not' })
class WhyNotPanelStub {}

describe('RiskPage', () => {
  let fixture: ComponentFixture<RiskPage>;
  let http: HttpTestingController;
  let chart: FakeChartEngine;
  let el: HTMLElement;
  let live: typeof LIVE;
  let mine: Record<string, number>;
  const selected = signal<string | null>('pf_2');
  const seen: string[] = [];

  beforeEach(() => {
    live = LIVE;
    mine = {};
    seen.length = 0;
    chart = new FakeChartEngine();
    TestBed.configureTestingModule({
      imports: [RiskPage],
      providers: [
        ...provideApi(),
        provideHttpClientTesting(),
        provideRouter([]),
        provideFakeChart(chart),
        {
          provide: PortfolioContextService,
          useValue: {
            selectedId: selected,
            live: signal(false),
            state: () => 'ready',
            noBook: () => false,
            query: () => (selected() ? { portfolio_id: selected() } : {}),
          },
        },
      ],
    });
    TestBed.overrideComponent(RiskPage, {
      remove: { imports: [WhyNotPanel] },
      add: { imports: [WhyNotPanelStub] },
    });
    http = TestBed.inject(HttpTestingController);
    fixture = TestBed.createComponent(RiskPage);
    el = fixture.nativeElement;
    fixture.detectChanges();
  });

  afterEach(() => http.verify());

  function respond(req: TestRequest): void {
    const url = new URL(req.request.urlWithParams, 'http://localhost');
    seen.push(url.pathname + url.search);
    switch (url.pathname) {
      case '/api/risk/live':
        return req.flush(live);
      case '/api/insights':
        return req.flush(INSIGHTS);
      case '/api/risk/policy':
        return req.flush(POLICY);
      case '/api/risk/limits':
        return req.flush({
          system: POLICY,
          mine,
          effective: { ...POLICY, ...mine },
          ignored: [],
        });
      case '/api/risk/snapshots':
        return req.flush(RISK_HISTORY);
      default:
        throw new Error(`unexpected request ${url.pathname}`);
    }
  }

  async function flushAll(): Promise<void> {
    for (let i = 0; i < 6; i++) {
      http.match(() => true).forEach(respond);
      await tick(5);
      fixture.detectChanges();
    }
  }

  it('shows each measure against its limit, with the status in words', async () => {
    await flushAll();
    const limits = el.querySelector('.limits')!;
    const text = limits.textContent ?? '';
    expect(text).toContain('Largest holding');
    expect(text).toContain('of 50.0%');
    expect(text).toContain('Near the limit');
    expect(text).toContain('Over the limit');
    expect(text).toContain('Weight in crypto');
    expect(limits.querySelectorAll('.limit-over').length).toBeGreaterThan(0);
  });

  it("shows today's VaR and ES and how often the model missed", async () => {
    await flushAll();
    const text = el.textContent ?? '';
    expect(text).toContain('2.00%');
    expect(text).toContain('3.30%');
    expect(text).toContain('3 misses, 1.2 times the expected rate');
    expect(text).not.toContain('Treat these numbers with care');
  });

  it('warns when the model is out of band, and shows a fading sleeve', async () => {
    live = {
      ...LIVE,
      portfolio: { ...LIVE.portfolio!, ratio_out_of_band: true, violation_ratio_95: 2.4 },
    };
    await flushAll();
    expect(el.textContent).toContain('Treat these numbers with care');
    expect(el.textContent).toContain('Fading. live IR 0.1 is under half the backtest IR 0.9');
  });

  it('draws the history and pages the readings for the picked portfolio', async () => {
    await flushAll();
    const series = chart.last!;
    expect(series.map((s) => s.id)).toEqual(['var95', 'es95', 'loss']);
    expect(series[0].points[0].time).toBe('2026-09-24');
    expect(el.textContent).toContain('2 days');
    const snapshotCalls = seen.filter((u) => u.startsWith('/api/risk/snapshots'));
    expect(snapshotCalls.some((u) => u.includes('limit=200'))).toBe(true);
    expect(snapshotCalls.some((u) => u.includes('limit=20&') || u.endsWith('limit=20'))).toBe(true);
    expect(snapshotCalls.every((u) => u.includes('portfolio_id=pf_2'))).toBe(true);
  });

  it('asks for a first trading run when there are no readings', async () => {
    live = { ...LIVE, portfolio: null, strategies: [], as_of: null };
    await flushAll();
    expect(el.textContent).toContain('No risk readings yet');
    expect(el.textContent).toContain('No strategy parts yet');
  });

  it('shows your own stricter limits in the rows and says so (area 5)', async () => {
    mine = { max_weight_per_ticker: 0.2, min_order_notional: 250 };
    await flushAll();
    const rows = [...el.querySelectorAll<HTMLElement>('.limits li')];
    const largest = rows.find((r) => r.textContent?.includes('Largest holding'))!;
    expect(largest.textContent).toContain('of 20.0%');
    expect(largest.querySelector('.yours')?.textContent).toContain('Your limit');
    const positions = rows.find((r) => r.textContent?.includes('Open positions'))!;
    expect(positions.querySelector('.yours')).toBeNull();
    const hint = el.querySelector('.limits + .hint')?.textContent ?? '';
    expect(hint).toContain('Rows marked Your limit');
    expect(hint).toContain('Buys worth less than $250.00 are skipped');
    expect(el.querySelector('a[href="/settings"]')?.textContent).toContain('risk limits');
  });

  it('names each strategy part in words, not by id', async () => {
    await flushAll();
    expect(el.textContent).toContain("Each strategy's part");
    expect(el.textContent).not.toContain('Strategy sleeves');
  });

  it('words misses without a ratio', async () => {
    await flushAll();
    expect(violationText(1, null, null)).toBe('1 miss');
  });

  it('never shows a raw p-value, and says plainly when misses beat chance (UX-69)', async () => {
    await flushAll();
    expect(el.textContent).not.toMatch(/\(p \d/);
    expect(violationText(3, 1.2, 0.61)).toBe('3 misses, 1.2 times the expected rate');
    expect(violationText(9, 3, 0.01)).toBe(
      '9 misses, 3 times the expected rate, more often than chance explains',
    );
  });
});
